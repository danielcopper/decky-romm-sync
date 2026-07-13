#!/usr/bin/env python3
"""Emulate the Deck's Game Mode metrics in the desktop dev loop's Big Picture window.

Part of the frontend dev loop (docs/contributing/frontend-dev-loop.md); driven by the
``dev:ui-scale`` mise task, usable standalone.

Why this exists: the QAM panel gets ~720 CSS px of height in a stock desktop Big Picture
where the Deck's internal panel gives ~454 — the dev loop shows vertical room the device
does not have, so a panel that fits on the desktop can overflow in Game Mode.

The emulation has TWO halves, and forcing the scale alone is NOT enough. The QAM's CSS
height follows::

    QAM CSS height = (Big Picture window PHYSICAL height / scale) - 80

so the scale is only half the equation: a fullscreen 2560x1440 Big Picture forced to 1.5
renders an 880 px QAM — nearly double the Deck's 454 — while reporting the Deck's scale.
This tool therefore ALSO sizes the Big Picture window to the Deck panel's 1280x800
physical pixels, and then HARD-VERIFIES the result against the expected QAM size rather
than claiming metrics it did not achieve.

Half 1 — the scale. The 1.5 is Steam's own per-display "GamepadUI display scale"
(Settings -> Display -> UI Scale), not gamescope and not a Chromium flag. Steam computes
it from the display's resolution + physical size and pushes it into each CEF browser view.
The same two undocumented calls Steam's settings UI uses drive it from here::

    SteamClient.Window.SetGamepadUIAutoDisplayScale(bool)
    SteamClient.Window.SetGamepadUIManualDisplayScaleFactor(float)

Unlike CDP's ``Emulation.setDeviceMetricsOverride`` (which fakes ``devicePixelRatio`` and
leaves the rest of the window unpainted), this is Steam's real scale: the views are
re-laid out and repainted for real.

Half 2 — the window. KWin scripting over DBus (the same loadScript/run/unloadScript route
``dev_open_bpm.sh`` uses for window placement) un-fullscreens the Big Picture window and
sets its geometry so the CLIENT area is exactly 1280x800, on whatever output it already
sits on. ``frameGeometry`` includes decorations, so the frame is corrected by the measured
frame-vs-client delta until the client area lands exactly. The window's prior geometry and
fullscreen state are captured first and restored on exit, alongside the scale.

The scale reaches only the views that are RENDERED when it is pushed. Steam's QuickAccess
view is created with the Big Picture window but laid out lazily, on the first QAM open of
that session: until then it measures 1x1 CSS. A view that renders after the push can come
up UNSCALED — measured at dpr 1, CSS 854x720, i.e. exactly the dev-loop lie this tool
exists to kill — and re-issuing the same two calls while it is live flips it to 1.5 /
854x454 on the spot (also measured). Whether a given view misses the push is not something
the tool can know up front, so the hold is a polling loop rather than a ``signal.pause()``:
it watches the QuickAccess view and re-pushes whenever it finds one rendering at the wrong
dpr, which also covers Steam re-materializing the popup later in the session. An unrendered
QAM is therefore not a failure — it is a not-yet, and it is reported as one (open the QAM
once and the tool verifies it).

DANGER — the forced scale PERSISTS: Steam flushes it to
``~/.local/share/Steam/config/config.vdf`` (``UI -> display -> Current -> ScaleFactor``)
within a couple of seconds. A factor left forced is a factor Steam keeps.

The setting is PER DISPLAY, keyed by Steam's display identity (``strDisplayName``, e.g.
``External: DP-2 27"|||Fullscreen-2560x1440``), and ``config.vdf`` carries one entry per
identity. So the blast radius depends on where the Big Picture window sits: on the Deck's
internal panel the identity is the one Game Mode itself uses and a forced factor bleeds
straight into Game Mode; on an external monitor it writes that monitor's entry and leaves
Game Mode's alone. The tool prints the identity it is scaling, so this is never a guess.

So the exit path CAPTURES the prior state before applying anything and puts back exactly
that (SIGINT/SIGTERM/finally): auto stays auto, and a manual UI Scale — an accessibility
"Larger text" value, say — is re-set to its old factor rather than silently flipped to
auto, which would destroy the user's setting. Only a hard SIGKILL can skip the restore;
``dev:ui-scale auto`` is the rescue path for exactly that case, and it is the one mode
that forces auto ON unconditionally (it has no captured state to honour).

The prior state is read from Steam's LIVE settings store (``window.settingsStore.settings``
in SharedJSContext — the same state Steam's own Display settings page renders), not from
``config.vdf``, and the restore is verified against it plus the live ``devicePixelRatio``.
``config.vdf`` is only a fallback source, because it is measurably unreliable in-session:
Steam re-flushes ``ScaleFactor`` within seconds but leaves ``AutoScaleFactor`` alone (it
stayed ``1`` on-device while a manual factor was applied and rendering), so the file can
report a manual scale as automatic — and restoring "automatic" to a user who is on a
manual scale is exactly the setting-destroying bug this capture exists to prevent.

Transport: the CEF remote-debugging endpoint on ``localhost:8080`` (a Decky platform
invariant — Decky Loader itself requires CEF debugging enabled). ``GET /json`` lists the
debuggable targets; ``SharedJSContext`` is where ``SteamClient`` lives, and the
``Big-Picture-*`` / ``QuickAccess_*`` page targets are the two views we measure.

The RFC 6455 client below is a deliberate copy of the one in
``py_modules/adapters/renderer_gc.py`` rather than a shared import: that adapter is
shipped plugin code with a fail-open, single-command, payload-discarding contract, while
this dev tool needs the evaluate *result*, several commands per run, and loud failures.
Sharing would mean either widening the shipped adapter's contract for a dev-only need or
adding a generic client to shipped ``lib/`` that no production path uses (it would land in
the release zip and in Sonar's scope). RFC 6455 is frozen; these ~80 lines don't drift.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any
from urllib.error import URLError
from urllib.request import urlopen

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import FrameType

_DEBUGGER_URL = "http://localhost:8080/json"
_SHARED_JS_CONTEXT = "SharedJSContext"
# RFC 6455 handshake GUID — appended to the client key to derive the expected
# Sec-WebSocket-Accept.
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
# Generous by dev-tool standards: a repaint of both views at a new scale is not a
# hot path, and a spurious timeout here would leave Steam on a forced scale.
_TIMEOUT_SEC = 8.0

_STEAM_CONFIG_VDF = os.path.expanduser("~/.local/share/Steam/config/config.vdf")

# Steam re-lays out the views asynchronously after the scale call: measured on-device, the
# new dpr is live at ~0.25 s but the layout is still mid-flight (a transient 570x480), and
# settles by ~0.5 s. Measure well past that or the printed numbers are a lie.
_SETTLE_SEC = 1.5

# The Deck's internal panel auto-scales to 1.5 — forcing it reproduces Game Mode's CSS
# metrics, but only once the window is the panel's size too (see _DECK_WINDOW).
_DECK_SCALE = 1.5
# The Deck's internal panel in PHYSICAL pixels. The window half of the emulation: the QAM's
# CSS height is (window physical height / scale) - 80, so the scale is meaningless until the
# window is this size. Measured: fullscreen 2560x1440 at 1.5 renders an 880 px QAM, not 454.
_DECK_WINDOW = (1280, 800)
# Steam lays a view out in CEIL(physical / scale) CSS px, and the QAM sits this far below
# Big Picture's header. Verified against three configurations: 800/1.5 - 80 = 454 (Game
# Mode, measured 454); 1440/1.5 - 80 = 880 (fullscreen 1440p at 1.5); 1440/1.9 - 80 = 678
# (fullscreen 1440p at Steam's automatic 1.9, measured 679).
_QAM_HEADER_CSS = 80
# The QAM popup is a fixed ~854 CSS px wide in every configuration measured — only its
# height tracks the window.
_QAM_CSS_WIDTH = 854
# Fractional-dpr rounding costs about a pixel (854 at 1.5, 855 at 1.9), so the emulation is
# "achieved" within a hair, not bit-exactly.
_QAM_TOLERANCE_PX = 3
# A view Steam has created but never laid out reports 1x1 CSS (measured on a Big Picture
# whose QAM has not been opened yet). The real QAM is ~854 wide in every configuration, so
# anything this small is "never rendered", not "rendered wrong" — a not-yet, not a failure.
_RENDERED_MIN_CSS_PX = 16
# Steam reports a scale of 1.9 as 1.899999976158142 (float32 round-trip), so dpr equality is
# a comparison within a hair, never ==.
_DPR_EPSILON = 0.01
# How often the hold re-checks the QuickAccess view for a late (unscaled) render. Cheap: one
# CDP evaluate against a view that is usually already correct.
_POLL_SEC = 2.0

_QAM_TITLE_PREFIX = "quickaccess"
_BPM_TITLE_MARKER = "bigpicture"

# KWin scripting (window half). Same DBus route dev_open_bpm.sh uses for placement.
_KWIN_SERVICE = "org.kde.KWin"
_KWIN_SCRIPTING_PATH = "/Scripting"
_KWIN_SCRIPT_NAME = "decky-romm-sync-ui-scale"
# A decorated window needs one correction pass (frame != client area); the rest is slack.
_GEOMETRY_ATTEMPTS = 3
# KWin resizes asynchronously and Steam repaints into the new size; measured well under
# this, but a short window is what makes the frame-vs-client delta readable.
_WINDOW_SETTLE_SEC = 0.6


class DevUiScaleError(RuntimeError):
    """Anything that stops the tool: no debugger, no target, a rejected CDP command."""


# --------------------------------------------------------------------------- CDP


def _list_targets() -> list[dict[str, Any]]:
    """Return the CEF debugger's target list, or raise with an actionable message."""
    try:
        with urlopen(_DEBUGGER_URL, timeout=_TIMEOUT_SEC) as resp:  # fixed localhost URL
            targets = json.load(resp)
    except (URLError, OSError, ValueError) as e:
        raise DevUiScaleError(
            f"no CEF debugger on {_DEBUGGER_URL} ({e}). Is Big Picture running? "
            "Start the dev loop with `mise run dev:watch`. If Steam is up but the endpoint "
            "is dead, ~/.steam/steam/.cef-enable-remote-debugging is missing (create it and "
            "restart Steam)."
        ) from e
    return [t for t in targets if isinstance(t, dict)]


def _normalize_title(title: str) -> str:
    return "".join(c for c in title.lower() if c.isalnum())


def _find_ws_url(targets: list[dict[str, Any]], match: str, *, prefix: bool) -> str | None:
    """Return the ``webSocketDebuggerUrl`` of the first target whose title matches.

    Titles are normalized (lowercased, non-alphanumerics stripped) before matching:
    the Big Picture target is localized (``Big-Picture-Modus`` on a German client), and
    the QAM target carries a per-window suffix (``QuickAccess_uid17``).
    """
    for target in targets:
        title = _normalize_title(str(target.get("title", "")))
        hit = title.startswith(match) if prefix else match in title
        if hit:
            url = target.get("webSocketDebuggerUrl")
            if isinstance(url, str):
                return url
    return None


def _evaluate(ws_url: str, expression: str) -> Any:
    """Run *expression* in the target behind *ws_url* and return its value.

    One short-lived connection per call: the tool sends a handful of commands over a
    run that can idle for minutes between them (apply -> hold -> restore), and a fresh
    socket for the restore is what makes the exit path robust after a long hold.
    """
    host, port, path = _parse_ws_url(ws_url)
    sock = socket.create_connection((host, port), timeout=_TIMEOUT_SEC)
    try:
        sock.settimeout(_TIMEOUT_SEC)
        if not _handshake(sock, host, port, path):
            raise DevUiScaleError("CEF rejected the WebSocket handshake")
        request = {
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True, "awaitPromise": True},
        }
        sock.sendall(_encode_text_frame(json.dumps(request)))
        reply = _await_reply(sock, request_id=1)
    finally:
        sock.close()

    if "exceptionDetails" in reply.get("result", {}):
        details = reply["result"]["exceptionDetails"]
        text = details.get("exception", {}).get("description") or details.get("text", "?")
        raise DevUiScaleError(f"CDP evaluate threw: {text}")
    if "error" in reply:
        raise DevUiScaleError(f"CDP error: {reply['error']}")
    return reply.get("result", {}).get("result", {}).get("value")


def _await_reply(sock: socket.socket, *, request_id: int) -> dict[str, Any]:
    """Read frames until the reply carrying *request_id* arrives."""
    for _ in range(16):  # bounded: no CDP domains are enabled, so events are not expected
        frame = _recv_frame(sock)
        if frame is None:
            raise DevUiScaleError("CDP connection closed before the reply arrived")
        opcode, payload = frame
        if opcode != 0x1:  # close / ping / continuation — not expected for this exchange
            raise DevUiScaleError(f"unexpected WebSocket frame (opcode {opcode:#x})")
        message = json.loads(payload)
        if message.get("id") == request_id:
            return message
    raise DevUiScaleError("no CDP reply for the evaluate request")


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
        header.append(0x80 | 126)  # mask bit + 16-bit extended length
        header.extend(struct.pack(">H", length))
    mask = os.urandom(4)
    header.extend(mask)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + masked


def _recv_frame(sock: socket.socket) -> tuple[int, bytes] | None:
    """Read one unmasked server frame; return ``(opcode, payload)`` or ``None`` on EOF."""
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
        ext = _recv_exact(sock, 8)
        if ext is None:
            return None
        length = struct.unpack(">Q", ext)[0]
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


# -------------------------------------------------------------------------- kwin

# One script, three actions. KWin scripts have no return channel over DBus, so the reply
# comes back through the user journal: print() from a KWin script lands there tagged
# `js:`, and the per-call token makes a reply unambiguous against a stale line.
_KWIN_JS = """
var TOKEN = "@TOKEN@";
var ACTION = "@ACTION@";
var PAYLOAD = @PAYLOAD@;

function emit(o) {
  print("[decky-uiscale:" + TOKEN + "] " + JSON.stringify(o));
}

function rect(r) {
  if (!r) {
    return null;
  }
  return {
    x: Math.round(r.x),
    y: Math.round(r.y),
    width: Math.round(r.width),
    height: Math.round(r.height),
  };
}

// Same heuristic as dev_open_bpm.sh: class "steam" covers the desktop client window and
// Big Picture; BPM keeps the "Big Picture" brand across locales ("Big-Picture-Modus"), and
// a fullscreen steam window is BPM as well (the client window is never fullscreen).
function isBigPicture(win) {
  if (!win || !win.resourceClass ||
      String(win.resourceClass).toLowerCase() !== "steam") {
    return false;
  }
  var caption = String(win.caption || "").toLowerCase();
  return caption.indexOf("picture") !== -1 || win.fullScreen === true;
}

function find() {
  var wins = workspace.windowList();
  for (var i = 0; i < wins.length; i++) {
    if (isBigPicture(wins[i])) {
      return wins[i];
    }
  }
  return null;
}

function state(win) {
  return {
    ok: true,
    output: win.output ? win.output.name : null,
    outputGeometry: win.output ? rect(win.output.geometry) : null,
    fullScreen: win.fullScreen === true,
    frame: rect(win.frameGeometry),
    client: rect(win.clientGeometry),
  };
}

try {
  var win = find();
  if (!win) {
    emit({ ok: false, error: "no Big Picture window in KWin's window list" });
  } else {
    if (ACTION === "resize") {
      // A fullscreen or maximized window ignores frameGeometry — clear both first.
      win.fullScreen = false;
      win.setMaximize(false, false);
      win.frameGeometry = PAYLOAD.frame;
    } else if (ACTION === "restore") {
      // Geometry BEFORE fullscreen: a window that is already fullscreen would swallow the
      // geometry write, and this order also leaves KWin the right un-fullscreen geometry.
      win.frameGeometry = PAYLOAD.frame;
      win.fullScreen = PAYLOAD.fullScreen === true;
    }
    emit(state(win));
  }
} catch (e) {
  emit({ ok: false, error: String(e) });
}
"""


@dataclass(frozen=True)
class Rect:
    """A window rectangle in physical pixels, as KWin reports it."""

    x: int
    y: int
    width: int
    height: int

    @classmethod
    def from_json(cls, raw: Any) -> Rect | None:
        if not isinstance(raw, dict):
            return None
        try:
            return cls(int(raw["x"]), int(raw["y"]), int(raw["width"]), int(raw["height"]))
        except (KeyError, TypeError, ValueError):
            return None

    def as_json(self) -> dict[str, int]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}

    def size(self) -> str:
        return f"{self.width}x{self.height}"


@dataclass(frozen=True)
class BpmWindow:
    """The Big Picture window as KWin sees it — the state the exit path has to put back.

    ``frame`` is the decorated rectangle (what ``frameGeometry`` writes); ``client`` is the
    content area Steam actually renders into, and the one that has to be the Deck's
    1280x800. The two differ by the decoration, which is why sizing is a measure-and-correct
    loop rather than a single write.
    """

    output: str | None
    output_geometry: Rect | None
    fullscreen: bool
    frame: Rect
    client: Rect

    def describe(self) -> str:
        where = self.output or "an unknown output"
        if self.fullscreen:
            return f"fullscreen {self.frame.size()} on {where}"
        return f"windowed {self.client.size()} on {where}"


def _qdbus_binary() -> str | None:
    """The qdbus binary to talk to KWin with; ``None`` when neither spelling exists."""
    for name in ("qdbus6", "qdbus"):
        found = shutil.which(name)
        if found is not None:
            return found
    return None


def _qdbus(binary: str, path: str, method: str, *args: str) -> str | None:
    """Call a KWin DBus method; ``None`` on any failure (the caller degrades, never crashes)."""
    try:
        result = subprocess.run(  # dev tool: not shipped plugin code
            [binary, _KWIN_SERVICE, path, method, *args],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _kwin_call(action: str, payload: dict[str, Any] | None = None) -> BpmWindow | None:
    """Run *action* ("capture" / "resize" / "restore") against the Big Picture window.

    ``None`` means KWin could not be driven or the window was not found — every caller
    degrades to a loud warning, and the emulation check then fails rather than lying.
    """
    binary = _qdbus_binary()
    if binary is None:
        return None
    token = os.urandom(4).hex()
    script = (
        _KWIN_JS.replace("@TOKEN@", token).replace("@ACTION@", action).replace("@PAYLOAD@", json.dumps(payload or {}))
    )
    if not _kwin_run_script(binary, script):
        return None
    reply = _kwin_read_reply(token)
    if reply is None or not reply.get("ok"):
        if isinstance(reply, dict) and reply.get("error"):
            print(f"WARNING: KWin: {reply['error']}", file=sys.stderr)
        return None

    frame = Rect.from_json(reply.get("frame"))
    client = Rect.from_json(reply.get("client"))
    if frame is None or client is None:
        return None
    output = reply.get("output")
    return BpmWindow(
        output=output if isinstance(output, str) else None,
        output_geometry=Rect.from_json(reply.get("outputGeometry")),
        fullscreen=bool(reply.get("fullScreen")),
        frame=frame,
        client=client,
    )


def _kwin_run_script(binary: str, script: str) -> bool:
    """Load, run and unload *script* in KWin. The script is short-lived by construction.

    It connects no signals — everything happens in the body during ``run`` — so unloading
    immediately is safe, and a re-run can never collide with a lingering copy. A fresh temp
    file per call sidesteps KWin's caching of an already-loaded path.
    """
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
        handle.write(script)
        path = handle.name
    try:
        _qdbus(binary, _KWIN_SCRIPTING_PATH, "org.kde.kwin.Scripting.unloadScript", _KWIN_SCRIPT_NAME)
        raw = _qdbus(binary, _KWIN_SCRIPTING_PATH, "org.kde.kwin.Scripting.loadScript", path, _KWIN_SCRIPT_NAME)
        script_id = (raw or "").strip()
        if not script_id.isdigit():
            return False
        return _qdbus(binary, f"{_KWIN_SCRIPTING_PATH}/Script{script_id}", "org.kde.kwin.Script.run") is not None
    finally:
        _qdbus(binary, _KWIN_SCRIPTING_PATH, "org.kde.kwin.Scripting.unloadScript", _KWIN_SCRIPT_NAME)
        os.unlink(path)


def _kwin_read_reply(token: str) -> dict[str, Any] | None:
    """Read the script's emitted line back out of the user journal; ``None`` if it never lands."""
    marker = f"[decky-uiscale:{token}]"
    deadline = time.monotonic() + _TIMEOUT_SEC
    while time.monotonic() < deadline:
        try:
            journal = subprocess.run(  # dev tool: not shipped plugin code
                ["journalctl", "--user", "--since=-2min", "--no-pager", "-o", "cat"],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_SEC,
                check=False,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
        for line in journal.splitlines():
            _, found, payload = line.partition(marker)
            if found:
                try:
                    return json.loads(payload)
                except ValueError:
                    return None
        time.sleep(0.15)
    return None


def _capture_window_state() -> BpmWindow | None:
    """Capture (and print) the Big Picture window's geometry BEFORE this run touches it."""
    window = _kwin_call("capture")
    if window is None:
        print(
            "WARNING: could not drive KWin — the Big Picture window keeps its current size.\n"
            "         Forcing the scale ALONE does not reproduce Deck metrics (the QAM's CSS height\n"
            "         follows window physical height / scale - 80), so the check below will fail.",
            file=sys.stderr,
        )
        return None
    print(f"Captured prior window: {window.describe()}")
    return window


def _deck_window_origin(prior: BpmWindow, width: int, height: int) -> tuple[int, int]:
    """Centre a *width* x *height* frame on the output the window is ALREADY on.

    Moving it between screens is dev_open_bpm.sh's job, not this tool's.
    """
    geometry = prior.output_geometry
    if geometry is None:
        return prior.frame.x, prior.frame.y
    return (
        geometry.x + max(0, (geometry.width - width) // 2),
        geometry.y + max(0, (geometry.height - height) // 2),
    )


def _size_window_to_deck(prior: BpmWindow) -> BpmWindow | None:
    """Size Big Picture so its CLIENT area is exactly the Deck panel's 1280x800.

    ``frameGeometry`` writes the DECORATED rectangle, so a single write lands the client
    area short by whatever the decoration adds. Correct by the measured frame-vs-client
    delta and re-assert until the client area is exact — which also means the tool never
    has to hardcode a decoration size, on any theme or compositor config.
    """
    target_w, target_h = _DECK_WINDOW
    frame_w, frame_h = target_w, target_h
    latest: BpmWindow | None = None
    for _ in range(_GEOMETRY_ATTEMPTS):
        x, y = _deck_window_origin(prior, frame_w, frame_h)
        frame = Rect(x, y, frame_w, frame_h)
        latest = _kwin_call("resize", {"frame": frame.as_json()})
        if latest is None:
            return None
        time.sleep(_WINDOW_SETTLE_SEC)
        delta_w = target_w - latest.client.width
        delta_h = target_h - latest.client.height
        if delta_w == 0 and delta_h == 0:
            return latest
        frame_w += delta_w
        frame_h += delta_h
    return latest


def _emulate_deck_window(prior: BpmWindow | None) -> BpmWindow | None:
    """Put the Big Picture window at the Deck panel's physical size; ``None`` if it could not be."""
    if prior is None:
        return None
    width, height = _DECK_WINDOW
    print(f"Sizing the Big Picture window to a {width}x{height} client area (was {prior.describe()})...")
    window = _size_window_to_deck(prior)
    if window is None:
        print("WARNING: KWin would not resize the Big Picture window — the check below will fail.", file=sys.stderr)
        return None
    print(f"  window now: {window.describe()} (frame {window.frame.size()})")
    return window


def _restore_window(prior: BpmWindow | None) -> None:
    """Put the window back exactly as it was — same geometry, same fullscreen state."""
    if prior is None:
        return
    window = _kwin_call("restore", {"frame": prior.frame.as_json(), "fullScreen": prior.fullscreen})
    if window is None:
        print(
            f"\nCOULD NOT RESTORE the Big Picture window ({prior.describe()}).\n"
            "It is still at the Deck's 1280x800 — resize or re-fullscreen it by hand.",
            file=sys.stderr,
        )
        return
    print(f"Restored: Big Picture window {window.describe()} (the state captured before this run).")
    if window.fullscreen != prior.fullscreen or (not prior.fullscreen and window.frame != prior.frame):
        print(
            f"WARNING: that is NOT the window state captured before this run ({prior.describe()}).",
            file=sys.stderr,
        )


# ------------------------------------------------------------------------- steam


def _parse_vdf(text: str) -> dict[str, Any]:
    """Parse Valve KeyValues text into nested dicts.

    Hand-rolled because the tool is stdlib-only and the file has two traps a line- or
    regex-based read falls into: keys carry escaped quotes (``External: eDP-1 7\\"|||…``)
    and some values span multiple lines (``SDL_GamepadBind``).
    """
    root: dict[str, Any] = {}
    stack: list[dict[str, Any]] = [root]
    pending_key: str | None = None
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            token, i = _read_quoted(text, i + 1)
            if pending_key is None:
                pending_key = token
            else:
                stack[-1][pending_key] = token
                pending_key = None
        elif c == "{":
            child: dict[str, Any] = {}
            stack[-1][pending_key or ""] = child
            stack.append(child)
            pending_key = None
            i += 1
        elif c == "}":
            if len(stack) > 1:
                stack.pop()
            i += 1
        elif c == "/" and text.startswith("//", i):
            i = text.find("\n", i)
            if i == -1:
                break
        else:
            i += 1
    return root


def _read_quoted(text: str, start: int) -> tuple[str, int]:
    """Read a quoted VDF token starting after the opening quote; return (token, next_index)."""
    out: list[str] = []
    i = start
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            out.append(text[i + 1])
            i += 2
            continue
        if c == '"':
            return "".join(out), i + 1
        out.append(c)
        i += 1
    return "".join(out), i


def _lookup(node: Any, *keys: str) -> Any:
    """Walk nested dicts case-insensitively (VDF key casing is not guaranteed)."""
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = next((v for k, v in node.items() if k.lower() == key.lower()), None)
    return node


@dataclass(frozen=True)
class SteamScaleState:
    """The UI Scale the user is on, plus the dpr the Big Picture view was rendering at.

    ``auto`` ON means Steam derives the factor from the display; OFF means the user picked
    a fixed one, and then ``factor`` is that choice — which is why the restore only replays
    ``factor`` when ``auto`` is OFF (under auto it is a value Steam *computed*, not a
    setting). ``dpr`` is what the view actually rendered at before the tool touched
    anything: the ground truth the restore is verified against.
    """

    auto: bool
    factor: float | None
    dpr: float | None
    source: str

    def describe(self) -> str:
        if self.auto:
            return "AUTOMATIC scaling"
        return f"MANUAL UI Scale {self.factor}"


def _read_live_scale_state() -> tuple[bool, float | None] | None:
    """Read the UI Scale from Steam's live settings store; ``None`` if unavailable.

    ``window.settingsStore.settings`` (in SharedJSContext) is what Steam's own Display
    settings page renders from — ``bDisplayIsUsingAutoScale`` is the authoritative auto
    flag and ``flCurrentDisplayScaleFactor`` the factor in force. This is the *live* state,
    which is what makes it trustworthy: ``config.vdf``'s ``AutoScaleFactor`` is not
    re-flushed in-session (measured on-device: it stayed ``1`` while a manual factor was
    applied and rendering), so the file can misreport a manual scale as automatic.
    """
    expression = """
    (() => {
      const s = window.settingsStore && window.settingsStore.settings;
      if (!s || typeof s.bDisplayIsUsingAutoScale !== 'boolean') return null;
      return JSON.stringify({
        auto: s.bDisplayIsUsingAutoScale,
        factor: s.flCurrentDisplayScaleFactor,
      });
    })()
    """
    try:
        ws_url = _find_ws_url(_list_targets(), _normalize_title(_SHARED_JS_CONTEXT), prefix=True)
        if ws_url is None:
            return None
        raw = _evaluate(ws_url, expression)
    except DevUiScaleError:
        return None
    if not isinstance(raw, str):
        return None
    state = json.loads(raw)
    factor = state.get("factor")
    return bool(state["auto"]), float(factor) if isinstance(factor, (int, float)) else None


def _read_display_name() -> str | None:
    """Steam's identity for the display it is currently scaling; ``None`` if unreadable.

    ``strDisplayName`` (e.g. ``External: DP-2 27"|||Fullscreen-2560x1440``) is the key
    ``config.vdf`` files the UI Scale under — one entry per display. Printing it is what
    makes the blast radius of a forced factor visible: the same string as Game Mode's means
    the force bleeds into Game Mode, a different one means it does not.
    """
    expression = """
    (() => {
      const s = window.settingsStore && window.settingsStore.settings;
      return s && typeof s.strDisplayName === 'string' ? s.strDisplayName : null;
    })()
    """
    try:
        ws_url = _find_ws_url(_list_targets(), _normalize_title(_SHARED_JS_CONTEXT), prefix=True)
        if ws_url is None:
            return None
        name = _evaluate(ws_url, expression)
    except DevUiScaleError:
        return None
    return name if isinstance(name, str) else None


def _read_config_vdf_scale_state() -> tuple[bool, float | None] | None:
    """Fallback read of ``UI -> display -> Current`` in ``config.vdf``; ``None`` if unreadable.

    Read-only. ``AutoScaleFactor "0"`` means a manual UI Scale. Only consulted when the
    live settings store is unavailable — see ``_read_live_scale_state`` for why the file
    is the weaker source.
    """
    try:
        with open(_STEAM_CONFIG_VDF, encoding="utf-8", errors="replace") as f:
            config = _parse_vdf(f.read())
    except OSError as e:
        print(f"WARNING: could not read {_STEAM_CONFIG_VDF} ({e})")
        return None

    current = _lookup(config, "InstallConfigStore", "UI", "display", "Current")
    if not isinstance(current, dict):
        print(f"WARNING: {_STEAM_CONFIG_VDF} has no UI/display/Current block")
        return None
    auto = str(_lookup(current, "AutoScaleFactor") or "1") != "0"
    raw = _lookup(current, "ScaleFactor")
    try:
        factor = float(str(raw))
    except (TypeError, ValueError):
        if not auto:
            print(f"WARNING: Steam's manual ScaleFactor is unreadable ({raw!r})")
            return None
        factor = None
    return auto, factor


def _read_scale_state() -> tuple[tuple[bool, float | None], str] | None:
    """The UI Scale in force, with the source it came from; ``None`` if neither can be read."""
    live = _read_live_scale_state()
    if live is not None:
        return live, "Steam's live settings store"
    print("WARNING: Steam's live settings store is unreadable — falling back to config.vdf")
    stored = _read_config_vdf_scale_state()
    return None if stored is None else (stored, "config.vdf")


def _capture_prior_state() -> SteamScaleState | None:
    """Capture (and print) the scale the user was on BEFORE this run.

    ``None`` means the prior state is unknown — the caller must then fall back to
    restoring automatic scaling and say out loud that it is a fallback.
    """
    read = _read_scale_state()
    if read is None:
        return None
    (auto, factor), source = read
    prior = SteamScaleState(auto=auto, factor=factor, dpr=_current_dpr(), source=source)
    dpr = "unknown" if prior.dpr is None else prior.dpr
    print(f"Captured prior state: {prior.describe()} at dpr {dpr} (source: {source})")
    return prior


def _steam_configured_scale() -> float:
    """Return the manual factor the user has set in Steam, or the deck default.

    Auto ON means there is no user-chosen number to adopt — fall back and say so.
    """
    read = _read_scale_state()
    if read is None:
        print(f"Could not read Steam's UI Scale — falling back to deck default {_DECK_SCALE}")
        return _DECK_SCALE
    (auto, factor), _ = read
    if auto or factor is None:
        print(f"Steam is on AUTOMATIC UI scaling (no manual factor set) — falling back to deck default {_DECK_SCALE}")
        return _DECK_SCALE
    print(f"Adopting the UI Scale you have set in Steam: {factor}")
    return factor


def _current_dpr() -> float | None:
    """The Big Picture view's live ``devicePixelRatio``; ``None`` if it can't be read."""
    try:
        ws_url = _find_ws_url(_list_targets(), _BPM_TITLE_MARKER, prefix=False)
        if ws_url is None:
            return None
        return float(str(_evaluate(ws_url, "String(devicePixelRatio)")))
    except (DevUiScaleError, ValueError):
        return None


def _set_scale(factor: float | None) -> None:
    """Force *factor*, or restore Steam's automatic scaling when *factor* is ``None``."""
    targets = _list_targets()
    ws_url = _find_ws_url(targets, _normalize_title(_SHARED_JS_CONTEXT), prefix=True)
    if ws_url is None:
        raise DevUiScaleError(
            f"no {_SHARED_JS_CONTEXT} debug target on {_DEBUGGER_URL}. Is Big Picture running? "
            "Start the dev loop with `mise run dev:watch`."
        )
    if factor is None:
        expression = "(() => { SteamClient.Window.SetGamepadUIAutoDisplayScale(true); return 'auto'; })()"
    else:
        expression = (
            "(() => {"
            "  SteamClient.Window.SetGamepadUIAutoDisplayScale(false);"
            f"  SteamClient.Window.SetGamepadUIManualDisplayScaleFactor({factor});"
            f"  return {factor};"
            "})()"
        )
    _evaluate(ws_url, expression)


@dataclass(frozen=True)
class ViewMetrics:
    """What a CEF view actually rendered — the ground truth every claim here is checked against."""

    dpr: float
    css_w: int
    css_h: int

    @property
    def physical(self) -> tuple[int, int]:
        """The view's size in real pixels — for Big Picture, the window's client area."""
        return round(self.css_w * self.dpr), round(self.css_h * self.dpr)

    @property
    def rendered(self) -> bool:
        """Has Steam ever laid this view out? A created-but-never-shown view reports 1x1 CSS."""
        return min(self.css_w, self.css_h) >= _RENDERED_MIN_CSS_PX

    def scaled_at(self, factor: float) -> bool:
        """Is this view rendering at *factor*? (Float-tolerant: Steam's 1.9 is 1.899999976158142.)"""
        return abs(self.dpr - factor) <= _DPR_EPSILON

    def describe(self) -> str:
        return f"dpr {self.dpr}  CSS {self.css_w}x{self.css_h}"


def _measure_view(match: str, *, prefix: bool) -> ViewMetrics | None:
    """Measure one CEF view's live CSS metrics; ``None`` when the view is not open."""
    ws_url = _find_ws_url(_list_targets(), match, prefix=prefix)
    if ws_url is None:
        return None
    expression = "JSON.stringify({dpr: window.devicePixelRatio, w: window.innerWidth, h: window.innerHeight})"
    try:
        raw = json.loads(str(_evaluate(ws_url, expression)))
        return ViewMetrics(float(raw["dpr"]), int(raw["w"]), int(raw["h"]))
    except (DevUiScaleError, KeyError, TypeError, ValueError):
        return None


def _expected_qam_css(factor: float) -> tuple[int, int]:
    """The QAM's CSS size the Deck renders at *factor*, from the 1280x800 panel.

    Steam lays a view out in CEIL(physical / factor) CSS px and insets the QAM below Big
    Picture's header, so 1.5 gives Game Mode's 854x454.
    """
    _, window_h = _DECK_WINDOW
    return _QAM_CSS_WIDTH, math.ceil(window_h / factor) - _QAM_HEADER_CSS


class Verdict(Enum):
    """The outcome of measuring the emulation against what a Deck renders.

    ``PENDING`` is the one that is neither of the other two: the window and the scale are
    right, but the QAM has never been opened in this Big Picture session, so Steam has not
    laid its view out and there is nothing to measure yet. Reporting that as ``FAILED``
    (which it was) cries wolf on the single most common way to start the tool — a fresh Big
    Picture — and trains the reader to ignore the one block that must never be ignored.
    """

    ACHIEVED = "achieved"
    PENDING = "pending"
    FAILED = "failed"


def _verify_emulation(factor: float, window: BpmWindow | None) -> Verdict:
    """Measure both views and check the emulation was actually ACHIEVED, not just requested.

    The whole point of the tool: a forced scale on a window that is not the Deck's size
    reports the Deck's dpr while rendering a QAM up to twice the Deck's height. So the
    numbers are compared against what the Deck would render, and a miss is a loud failure —
    never a quietly-wrong "measured" line the reader would take for a success.
    """
    bpm = _measure_view(_BPM_TITLE_MARKER, prefix=False)
    qam = _measure_view(_QAM_TITLE_PREFIX, prefix=True)
    for label, view in (("Big Picture", bpm), ("QuickAccess", qam)):
        if view is None:
            print(f"  {label:<12} target not found (view not created yet)")
        elif not view.rendered:
            print(f"  {label:<12} {view.describe()} — created, never rendered")
        else:
            print(f"  {label:<12} {view.describe()}")

    expected_w, expected_h = _expected_qam_css(factor)
    if qam is None or not qam.rendered:
        _report_pending(factor, bpm)
        return Verdict.PENDING

    if abs(qam.css_w - expected_w) <= _QAM_TOLERANCE_PX and abs(qam.css_h - expected_h) <= _QAM_TOLERANCE_PX:
        print(
            f"  => QAM is {qam.css_w}x{qam.css_h} CSS at dpr {qam.dpr}, the {expected_w}x{expected_h} "
            f"a Deck renders at scale {factor} — this is what the Deck renders."
        )
        return Verdict.ACHIEVED

    window_size = _window_physical_size(window, bpm)
    deck_w, deck_h = _DECK_WINDOW
    print(
        f"\nEMULATION FAILED — these are NOT Deck metrics.\n"
        f"  QAM measured: {qam.css_w}x{qam.css_h} CSS (expected {expected_w}x{expected_h} at scale {factor})\n"
        f"  Big Picture window: {window_size} physical (must be {deck_w}x{deck_h} — the Deck's panel)\n"
        f"  The QAM's CSS height is (window physical height / scale) - {_QAM_HEADER_CSS}, so forcing the\n"
        f"  scale on a window of the wrong size gives the Deck's dpr with the wrong layout.\n"
        f"  Most likely: the window could not be resized (KWin unreachable, or Steam re-asserted its\n"
        f"  size). Any layout judgement made on these numbers is invalid.",
        file=sys.stderr,
    )
    return Verdict.FAILED


def _report_pending(factor: float, bpm: ViewMetrics | None) -> None:
    """Say that the QAM has not been rendered yet — a not-yet, not a failure.

    Everything the tool controls (window size, forced scale) has landed; the one thing it
    cannot do is open the QAM for the user. Steam lays that view out lazily, on the first
    open of the session, so there is nothing to measure until then.
    """
    expected_w, expected_h = _expected_qam_css(factor)
    deck_w, deck_h = _DECK_WINDOW
    applied = "at the forced scale" if bpm is None else f"at dpr {bpm.dpr}"
    print(
        f"\nQAM NOT VERIFIED YET — the emulation is applied, the QAM has simply never been opened.\n"
        f"  Big Picture: {deck_w}x{deck_h} physical, scale forced to {factor} (rendering {applied}).\n"
        f"  Steam creates the QuickAccess view with the window but lays it out only on the FIRST\n"
        f"  QAM open of a Big Picture session — until then it is 1x1, and Steam's scale push reaches\n"
        f"  only rendered views, so a QAM opened later can come up unscaled (dpr 1).\n"
        f"  => OPEN THE QAM ONCE. This tool re-applies the scale to it the moment it renders, and\n"
        f"     then verifies it against the {expected_w}x{expected_h} a Deck shows at scale {factor}."
    )


def _hold(factor: float, window: BpmWindow | None, *, verified: bool) -> None:
    """Hold until Ctrl-C, re-applying the scale to any QuickAccess view that renders late.

    This is the loop the tool cannot do without. Steam's scale push reaches only the views
    that are RENDERED when it lands, and the QuickAccess view is laid out lazily on the
    first QAM open of a Big Picture session — so a QAM opened after the force can come up at
    dpr 1 / 854x720 (measured), which is precisely the dev-loop lie the tool exists to kill.
    Re-issuing the same two calls while that view is live flips it to dpr 1.5 / 854x454
    immediately (measured), and the same applies whenever Steam re-materializes the popup.

    Quiet by construction: it re-applies only when a RENDERED view is at the wrong dpr, and
    only once per distinct measurement — so a re-apply that does not take (Steam gone, say)
    prints once instead of every two seconds.
    """
    acted_on: tuple[float, int, int] | None = None
    while True:
        time.sleep(_POLL_SEC)  # KeyboardInterrupt lands here; the caller's finally restores
        qam = _measure_view(_QAM_TITLE_PREFIX, prefix=True)
        if qam is None or not qam.rendered:
            continue  # the QAM has still never been opened — nothing to scale, nothing to say
        if qam.scaled_at(factor):
            if not verified:  # it rendered already correct: verify once, then go quiet
                _reverify(factor, window)
                verified = True
            continue
        measurement = (qam.dpr, qam.css_w, qam.css_h)
        if measurement == acted_on:
            continue  # already re-applied for exactly this view state and it did not take
        acted_on = measurement
        print(
            f"\nQuickAccess view is at {qam.describe()} — not the forced {factor}.\n"
            f"Re-applying the scale (Steam's push reaches only the views that are rendered when it lands)..."
        )
        _set_scale(factor)
        time.sleep(_SETTLE_SEC)
        _reverify(factor, window)
        verified = True


def _reverify(factor: float, window: BpmWindow | None) -> None:
    """Re-run the full verification and print the verdict — same numbers, same loud failure."""
    print("\nMeasured (real, repainted — not a CDP emulation override):")
    _verify_emulation(factor, window)


def _window_physical_size(window: BpmWindow | None, bpm: ViewMetrics | None) -> str:
    """The Big Picture window's real pixel size, for the failure block; KWin first, CEF as fallback."""
    if window is not None:
        return window.client.size()
    if bpm is not None:
        width, height = bpm.physical
        return f"~{width}x{height}"
    return "unknown"


# -------------------------------------------------------------------------- main


_USAGE = """usage: dev_ui_scale.py [deck | <factor> | steam | auto]

Emulates the Deck by BOTH sizing the Big Picture window to the panel's 1280x800 physical
pixels AND forcing Steam's GamepadUI display scale — the QAM's CSS height is
(window physical height / scale) - 80, so neither half works alone. The result is then
verified against what the Deck would render, and a miss is reported as a failure.

  deck      (default) force 1.5 — the Deck internal panel's auto scale, i.e. exact
            Game Mode metrics (QAM 854x454).
  <factor>  force a bare number. 2.0-2.4 = a user on "Larger text" (worst case: least
            vertical room), ~1.28 = docked 1080p, ~1.71 = docked 1440p.
  steam     adopt the UI Scale the user currently has set in Steam (config.vdf); falls
            back to 1.5 when Steam is on automatic scaling.
  auto|off  UNCONDITIONALLY re-enable Steam's automatic scaling and exit. This is the
            rescue path for when a previous run was SIGKILLed and its captured state
            died with it — it does NOT know what you were on, it just forces auto (and
            it does not touch the window; resize it by hand).

The forcing modes hold until Ctrl-C, then restore what you were on BEFORE the run: auto
stays auto, a manual UI Scale (e.g. an accessibility "Larger text" value) is put back
exactly as it was rather than silently flipped to auto, and the Big Picture window goes
back to the geometry and fullscreen state it had."""


def _parse_mode(argv: list[str]) -> float | None:
    """Map the single positional arg to a factor, or ``None`` for restore-and-exit."""
    if not argv:
        return _DECK_SCALE
    arg = argv[0].strip().lower()
    if arg in ("-h", "--help"):
        print(_USAGE)
        raise SystemExit(0)
    if arg in ("auto", "off"):
        return None
    if arg == "deck":
        return _DECK_SCALE
    if arg == "steam":
        return _steam_configured_scale()
    try:
        factor = float(arg)
    except ValueError:
        raise DevUiScaleError(f"unknown mode {argv[0]!r}\n\n{_USAGE}") from None
    if not 0.5 <= factor <= 2.5:
        raise DevUiScaleError(f"factor {factor} is outside Steam's 0.5-2.5 UI Scale range")
    return factor


def _on_signal(_signum: int, _frame: FrameType | None) -> None:
    """Turn SIGTERM into the same unwind Ctrl-C takes, so the ``finally`` restore runs."""
    raise KeyboardInterrupt


def _restore(prior: SteamScaleState | None, prior_window: BpmWindow | None) -> None:
    """Put the scale AND the window back — exactly. Never raises: it runs in a ``finally``.

    Scale before window, which is the reverse of the apply order and the only safe order for
    two independent reasons. First, the UI Scale is filed per display identity and the
    identity depends on the window (Steam keys it ``…|||Fullscreen-2560x1440`` fullscreen but
    ``…|||Windowed`` once resized), so un-forcing while the window is still at the Deck's size
    is what clears the entry the force was actually written into. Second, it is the right risk
    order: a stranded forced scale is the damaging outcome (Steam persists it), a window left
    at 1280x800 is cosmetic.

    Signals are held off for the duration. The window half is the slow half — subprocesses
    plus a journal read — and an impatient second Ctrl-C used to truncate the restore right
    there, leaving the window resized. SIGKILL remains the only way to skip the restore, which
    is what the docs promise.
    """
    with _signals_held_off():
        _restore_scale(prior)
        _restore_window(prior_window)
        _verify_restore(prior)


@contextmanager
def _signals_held_off() -> Iterator[None]:
    """Ignore SIGINT/SIGTERM inside the block, then put the previous handlers back."""
    previous = {number: signal.signal(number, signal.SIG_IGN) for number in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for number, handler in previous.items():
            signal.signal(number, handler)


def _restore_scale(prior: SteamScaleState | None) -> None:
    """Put *prior* back — exactly.

    A user who deliberately set a manual UI Scale (an accessibility "Larger text" value,
    say) must get that scale back, not Steam's automatic one — flipping them to auto on
    exit would silently destroy their setting. Automatic scaling is restored only when
    that is what they were on, or as an announced fallback when the prior state is unknown.
    """
    try:
        if prior is None:
            _set_scale(None)
            print(
                "FALLBACK: prior state was unknown, so Steam's AUTOMATIC scaling was restored.\n"
                "If you had a manual UI Scale set, re-set it in Steam (Settings -> Display -> UI Scale).",
                file=sys.stderr,
            )
        elif prior.auto:
            _set_scale(None)
            print("Restored: AUTOMATIC scaling (the state captured before this run).")
        else:
            _set_scale(prior.factor)
            print(f"Restored: MANUAL UI Scale {prior.factor} (the state captured before this run).")
    except DevUiScaleError as e:
        target = "automatic scaling" if prior is None or prior.auto else f"manual UI Scale {prior.factor}"
        print(
            f"\nCOULD NOT RESTORE {target} ({e}).\n"
            "Steam is STILL on the forced scale, and Steam persists it — fix it with:\n"
            "  mise run dev:ui-scale auto",
            file=sys.stderr,
        )


def _verify_restore(prior: SteamScaleState | None) -> None:
    """Check the restore actually landed, rather than just claiming it did.

    Verified against the live settings store and the live ``devicePixelRatio`` — never
    against ``config.vdf``, whose ``AutoScaleFactor`` is not re-flushed in-session and so
    cannot tell an auto restore from a forced scale.
    """
    time.sleep(_SETTLE_SEC)
    dpr = _current_dpr()
    live = _read_live_scale_state()
    now = "an unreadable state" if live is None else SteamScaleState(live[0], live[1], dpr, "live").describe()
    print(f"Verified: Steam is on {now}, rendering at dpr {'unknown' if dpr is None else dpr}")
    if prior is None:
        return

    auto_drifted = live is not None and live[0] != prior.auto
    factor_drifted = live is not None and not prior.auto and live[1] != prior.factor
    dpr_drifted = prior.dpr is not None and dpr is not None and abs(dpr - prior.dpr) > 0.01
    if auto_drifted or factor_drifted or dpr_drifted:
        print(
            f"WARNING: that is NOT the state captured before this run ({prior.describe()} at dpr {prior.dpr}).\n"
            "The restore did not land — check Steam -> Settings -> Display -> UI Scale.",
            file=sys.stderr,
        )


def main(argv: list[str]) -> int:
    # Piped stdout (which is how `mise run` invokes this) is block-buffered by default, so
    # the progress lines would surface out of order against the unbuffered stderr warnings —
    # and a warning that appears before the action it refers to is worse than no warning.
    sys.stdout.reconfigure(line_buffering=True)
    try:
        factor = _parse_mode(argv)
    except DevUiScaleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    # The rescue path: no prior state to honour (the run that had it was killed), so this
    # deliberately forces auto ON rather than restoring anything.
    if factor is None:
        try:
            _set_scale(None)
        except DevUiScaleError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print("Steam's automatic UI scaling force-enabled (rescue mode).")
        return 0

    signal.signal(signal.SIGTERM, _on_signal)
    # Capture BEFORE applying — this is the state the exit path has to put back. Both halves
    # of it: the scale Steam is on, and the window KWin is about to resize.
    prior = _capture_prior_state()
    prior_window = _capture_window_state()
    try:
        window = _emulate_deck_window(prior_window)
        print(f"Forcing GamepadUI display scale {factor} (auto scaling OFF)...")
        _set_scale(factor)
        time.sleep(_SETTLE_SEC)
        print(f"  scaling display: {_read_display_name() or 'unknown'}")
        print("\nMeasured (real, repainted — not a CDP emulation override):")
        verdict = _verify_emulation(factor, window)
        print(
            f"\nHOLDING — Ctrl-C restores what you were on before this run\n"
            f"  scale:  {_describe(prior)}\n"
            f"  window: {prior_window.describe() if prior_window else 'unknown — will be left as-is'}\n"
            "The QuickAccess view is watched while this holds: Steam's scale reaches only the views\n"
            "that are rendered when it is pushed, so a QAM opened later is re-scaled here.\n"
            "WARNING: Steam persists this scale to config.vdf, filed under the display identity\n"
            "printed above. A hard kill (SIGKILL) skips the restore — recover with:\n"
            "  mise run dev:ui-scale auto"
        )
        _hold(factor, window, verified=verdict is not Verdict.PENDING)
    except KeyboardInterrupt:
        print("\nRestoring...")
    except DevUiScaleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        # Covers the apply itself: the forcing expression sets auto=false before it sets
        # the factor, so even a half-applied scale must be undone — and a window that was
        # resized before a later step failed must still go back.
        _restore(prior, prior_window)
    return 0


def _describe(prior: SteamScaleState | None) -> str:
    return "unknown — will fall back to automatic" if prior is None else prior.describe()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
