#!/usr/bin/env python3
"""Force Steam's GamepadUI display scale so the desktop dev loop renders Game Mode metrics.

Part of the frontend dev loop (docs/contributing/frontend-dev-loop.md); driven by the
``dev:ui-scale`` mise task, usable standalone.

Why this exists: the windowed Big Picture of ``mise run dev:watch`` renders at device
pixel ratio 1, so the QAM panel gets ~720 CSS px of height where the Deck's internal
panel (dpr 1.5) gives ~454. The dev loop shows ~59% more vertical room than the device —
a panel that fits on the desktop can overflow in Game Mode. CSS *width* is 854 in both,
so only height lies.

The 1.5 is Steam's own per-display "GamepadUI display scale" (Settings -> Display -> UI
Scale), not gamescope and not a Chromium flag. Steam computes it from the display's
resolution + physical size and pushes it into each CEF browser view. The same two
undocumented calls Steam's settings UI uses drive it from here::

    SteamClient.Window.SetGamepadUIAutoDisplayScale(bool)
    SteamClient.Window.SetGamepadUIManualDisplayScaleFactor(float)

Unlike CDP's ``Emulation.setDeviceMetricsOverride`` (which fakes ``devicePixelRatio`` and
leaves the rest of the window unpainted), this is Steam's real scale: the views are
re-laid out and repainted for real.

DANGER — the forced scale PERSISTS: Steam flushes it to
``~/.local/share/Steam/config/config.vdf`` (``UI -> display -> Current -> ScaleFactor``)
within a couple of seconds, keyed by a display identity the Deck shares with Game Mode. A
factor left forced is a factor Steam keeps.

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
import os
import signal
import socket
import struct
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.error import URLError
from urllib.request import urlopen

if TYPE_CHECKING:
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

# The Deck's internal panel auto-scales to 1.5 — forcing it on the desktop reproduces
# Game Mode's CSS metrics exactly.
_DECK_SCALE = 1.5
# Game Mode reference metrics (measured on the Deck's internal panel, dpr 1.5).
_GAME_MODE_QAM = (854, 454)

_QAM_TITLE_PREFIX = "quickaccess"
_BPM_TITLE_MARKER = "bigpicture"


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


def _measure() -> None:
    """Print the real CSS metrics of both views, next to the Game Mode reference."""
    targets = _list_targets()
    views = (
        ("Big Picture", _find_ws_url(targets, _BPM_TITLE_MARKER, prefix=False)),
        ("QuickAccess", _find_ws_url(targets, _QAM_TITLE_PREFIX, prefix=True)),
    )
    expression = "JSON.stringify({dpr: window.devicePixelRatio, w: window.innerWidth, h: window.innerHeight})"
    qam: dict[str, Any] | None = None
    for label, ws_url in views:
        if ws_url is None:
            print(f"  {label:<12} target not found (view not open?)")
            continue
        metrics = json.loads(str(_evaluate(ws_url, expression)))
        print(f"  {label:<12} dpr {metrics['dpr']}  CSS {metrics['w']}x{metrics['h']}")
        if label == "QuickAccess":
            qam = metrics

    ref_w, ref_h = _GAME_MODE_QAM
    if qam is None:
        print(f"  (Game Mode reference: QAM {ref_w}x{ref_h})")
    elif (qam["w"], qam["h"]) == (ref_w, ref_h):
        print(f"  => QAM matches the Game Mode reference ({ref_w}x{ref_h}) — what you see is what the Deck renders.")
    else:
        delta = qam["h"] - ref_h
        sign = "+" if delta >= 0 else ""
        print(
            f"  => QAM is {qam['w']}x{qam['h']} vs the Game Mode reference {ref_w}x{ref_h} "
            f"({sign}{delta} px of height)."
        )


# -------------------------------------------------------------------------- main


_USAGE = """usage: dev_ui_scale.py [deck | <factor> | steam | auto]

  deck      (default) force 1.5 — the Deck internal panel's auto scale, i.e. exact
            Game Mode metrics (QAM 854x454).
  <factor>  force a bare number. 2.0-2.4 = a user on "Larger text" (worst case: least
            vertical room), ~1.28 = docked 1080p, ~1.71 = docked 1440p.
  steam     adopt the UI Scale the user currently has set in Steam (config.vdf); falls
            back to 1.5 when Steam is on automatic scaling.
  auto|off  UNCONDITIONALLY re-enable Steam's automatic scaling and exit. This is the
            rescue path for when a previous run was SIGKILLed and its captured state
            died with it — it does NOT know what you were on, it just forces auto.

The forcing modes hold until Ctrl-C, then restore the scale you were on BEFORE the run:
auto stays auto, and a manual UI Scale (e.g. an accessibility "Larger text" value) is put
back exactly as it was — never silently flipped to auto."""


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


def _restore(prior: SteamScaleState | None) -> None:
    """Put *prior* back — exactly. Never raises: it runs in a ``finally``.

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
        return
    _verify_restore(prior)


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
    # Capture BEFORE applying — this is the state the exit path has to put back.
    prior = _capture_prior_state()
    print(f"Forcing GamepadUI display scale {factor} (auto scaling OFF)...")
    try:
        _set_scale(factor)
        # The QAM is a popup view Steam can recreate at will. Steam's own scale (unlike a
        # CDP Emulation override, which is per-view and dies with the view) is display
        # state that any recreated view picks up — so there is nothing to re-apply here.
        # Do not "fix" this by adding a polling loop.
        time.sleep(_SETTLE_SEC)
        print("\nMeasured (real, repainted — not a CDP emulation override):")
        _measure()
        print(
            f"\nHOLDING — Ctrl-C restores what you were on before this run ({_describe(prior)}).\n"
            "WARNING: Steam persists this scale to config.vdf, under the same display identity\n"
            "Game Mode uses. A hard kill (SIGKILL) skips the restore — recover with:\n"
            "  mise run dev:ui-scale auto"
        )
        signal.pause()
    except KeyboardInterrupt:
        print("\nRestoring...")
    except DevUiScaleError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        # Covers the apply itself: the forcing expression sets auto=false before it sets
        # the factor, so even a half-applied scale must be undone.
        _restore(prior)
    return 0


def _describe(prior: SteamScaleState | None) -> str:
    return "unknown — will fall back to automatic" if prior is None else prior.describe()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
