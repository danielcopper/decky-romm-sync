"""Game-process adapter — concrete ``GameProcessControl`` over ``/proc``.

Owns the host-side view of what a flatpak app is running: the per-user flatpak
instance registry, the ``/proc`` process tree beneath each live instance, and
the signals sent to the processes found there. Direct file reads and
``os.kill`` only — no ``subprocess``, no shelling out to ``flatpak kill``.

Why the host process table at all — Steam cannot terminate these games.
A RomM shortcut execs ``flatpak run <app>``; flatpak's D-Bus portal starts the
sandbox from the session helper, so the emulator is **not** a descendant of
Steam's ``reaper`` and ``SteamClient.Apps.TerminateApp`` has nothing to signal
(measured on-device: a proven no-op). The flatpak instance registry is the only
handle that survives that detach.

Every read is guarded and fail-soft, like ``renderer_rss``: a process or
instance directory that vanishes mid-scan is a normal race and is skipped, so
one dead pid never blanks the whole result. A pid that cannot be identified is
never signalled.
"""

from __future__ import annotations

import json
import os
import signal

_PROC = "/proc"

# Per-user flatpak instance registry: ``$XDG_RUNTIME_DIR/.flatpak/<instance>/``,
# one directory per LIVE instance, torn down when the instance exits. The
# runtime dir is derived from the real uid rather than the environment because
# the plugin backend runs headless, where ``XDG_RUNTIME_DIR`` may be unset.
_RUNTIME_BASE = "/run/user"
_FLATPAK_INSTANCES = ".flatpak"

# ``<instance>/info`` is INI-ish; flatpak writes the app id as the ``name`` key
# of its ``[Application]`` section. ``<instance>/bwrapinfo.json`` carries the
# host pid of the instance's inner ``bwrap`` under ``child-pid``.
_INFO_FILE = "info"
_NAME_KEY = "name"
_BWRAP_INFO_FILE = "bwrapinfo.json"
_CHILD_PID_KEY = "child-pid"

# ``bwrap`` is the sandbox scaffolding, never the game. The descent passes
# through it, but it is never returned as a signal target: killing it collapses
# the sandbox under the emulator instead of letting the emulator flush its save.
_BWRAP_COMM = "bwrap"

# Process states that mean "already exited": ``Z`` is a zombie awaiting reaping
# and ``X``/``x`` is a dead task. Neither will ever run code again.
_EXITED_STATES = frozenset({"Z", "X", "x"})

# Depth cap on the ``/proc`` descent. The real tree is shallow (bwrap → the
# RetroDECK launcher shell → the emulator); the cap only bounds a pathological
# read of a process table being rewritten underneath us.
_MAX_DEPTH = 16


class GameProcessAdapter:
    """Real ``GameProcessControl`` backed by the flatpak instance registry and ``/proc``."""

    def find_game_pids(self, flatpak_app_id: str) -> list[int]:
        """Return *flatpak_app_id*'s live host PIDs, deepest process first.

        Resolves every live instance of the app through the per-user flatpak
        instance registry, takes each instance's inner ``bwrap`` pid, and walks
        that pid's ``/proc`` descendants. ``bwrap`` processes and pids whose
        identity cannot be read are excluded. Empty when the app is not running
        or the registry is unreadable.
        """
        pids: list[int] = []
        seen: set[int] = set()
        for root_pid in self._instance_root_pids(flatpak_app_id):
            for pid in self._descendants(root_pid):
                if pid not in seen:
                    seen.add(pid)
                    pids.append(pid)
        return pids

    def request_stop(self, pid: int) -> bool:
        """Send one ``SIGTERM`` to *pid*, returning whether it was delivered.

        Delivery only — this method has no memory and no retry; sending exactly
        one stop request per process is the caller's contract (and a hard save-
        safety rule; see ``services.game_process``).
        """
        return self._signal(pid, signal.SIGTERM)

    def force_kill(self, pid: int) -> bool:
        """Send ``SIGKILL`` to *pid*, returning whether it was delivered."""
        return self._signal(pid, signal.SIGKILL)

    def is_alive(self, pid: int) -> bool:
        """Return True while *pid* is a live process, reading ``/proc/<pid>/status``.

        A zombie or dead task reads as **not** alive: it has already run its
        exit path, so treating it as running would spend the caller's whole
        grace window waiting for something that will never change. An
        unreadable ``status`` means the process is gone — the normal exit.
        """
        try:
            with open(f"{_PROC}/{pid}/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("State:"):
                        # Line shape: "State:\tS (sleeping)" — the second field
                        # is the single-letter state code.
                        return line.split()[1] not in _EXITED_STATES
        except (OSError, UnicodeDecodeError, IndexError):
            return False
        return False

    # ── flatpak instance registry ────────────────────────────────────────────

    def _instance_root_pids(self, flatpak_app_id: str) -> list[int]:
        """Return the inner-``bwrap`` pid of every live instance of *flatpak_app_id*."""
        base = os.path.join(_RUNTIME_BASE, str(os.getuid()), _FLATPAK_INSTANCES)
        try:
            entries = os.listdir(base)
        except OSError:
            return []
        roots: list[int] = []
        for entry in entries:
            instance_dir = os.path.join(base, entry)
            if not self._instance_runs_app(instance_dir, flatpak_app_id):
                continue
            child_pid = self._instance_child_pid(instance_dir)
            if child_pid is not None:
                roots.append(child_pid)
        return roots

    @staticmethod
    def _instance_runs_app(instance_dir: str, flatpak_app_id: str) -> bool:
        """Return True when *instance_dir*'s ``info`` names *flatpak_app_id*.

        Matched on the whole ``name=<app id>`` line, so no other key's value and
        no longer app id sharing the prefix can be mistaken for a hit. Any read
        failure reads as "not this app": an instance directory is removed the
        moment the instance exits, so a vanished or half-written one is a normal
        race, not an error.
        """
        target = f"{_NAME_KEY}={flatpak_app_id}"
        try:
            with open(os.path.join(instance_dir, _INFO_FILE), encoding="utf-8") as f:
                return any(line.strip() == target for line in f)
        except (OSError, UnicodeDecodeError):
            return False

    @staticmethod
    def _instance_child_pid(instance_dir: str) -> int | None:
        """Return the host pid in *instance_dir*'s ``bwrapinfo.json``, or None.

        None covers every unusable case — the file missing (the instance exited),
        unparseable JSON (it is written non-atomically as the sandbox starts),
        or a ``child-pid`` that is absent or not a positive integer.
        """
        try:
            with open(os.path.join(instance_dir, _BWRAP_INFO_FILE), encoding="utf-8") as f:
                payload = json.load(f)
        # ``ValueError`` covers both ``json.JSONDecodeError`` and the
        # ``UnicodeDecodeError`` a non-UTF-8 read raises — both subclass it.
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        child_pid = payload.get(_CHILD_PID_KEY)
        # ``bool`` is an ``int`` subclass — exclude it explicitly so a JSON
        # ``true`` can never be signalled as pid 1.
        if isinstance(child_pid, bool) or not isinstance(child_pid, int) or child_pid <= 0:
            return None
        return child_pid

    # ── /proc process tree ───────────────────────────────────────────────────

    def _descendants(self, root_pid: int) -> list[int]:
        """Return *root_pid*'s subtree as signal targets, deepest level first.

        Breadth-first by level, emitted in reverse level order: the emulator
        (the deepest process) is handed back before the shell wrappers whose
        exit would tear it down mid-write. The visited set makes a process table
        that mutates during the walk harmless, and ``_MAX_DEPTH`` bounds it.
        """
        levels: list[list[int]] = []
        seen = {root_pid}
        frontier = [root_pid]
        for _ in range(_MAX_DEPTH):
            if not frontier:
                break
            level = [pid for pid in frontier if self._is_signal_target(pid)]
            if level:
                levels.append(level)
            next_frontier: list[int] = []
            for pid in frontier:
                for child in self._children(pid):
                    if child not in seen:
                        seen.add(child)
                        next_frontier.append(child)
            frontier = next_frontier
        return [pid for level in reversed(levels) for pid in level]

    @staticmethod
    def _children(pid: int) -> list[int]:
        """Return *pid*'s direct child PIDs from ``/proc/<pid>/task/<pid>/children``.

        Empty on any read failure — a process that exits between the parent scan
        and this read simply has no children to contribute.
        """
        try:
            with open(f"{_PROC}/{pid}/task/{pid}/children", encoding="utf-8") as f:
                raw = f.read()
        except (OSError, UnicodeDecodeError):
            return []
        # Space-separated pid list, possibly with a trailing space and newline.
        return [int(token) for token in raw.split() if token.isdigit()]

    @staticmethod
    def _is_signal_target(pid: int) -> bool:
        """Return True when *pid* is a process the stop ladder may signal.

        False for the ``bwrap`` sandbox scaffolding, and false for any pid whose
        ``comm`` cannot be read — an unidentifiable process is never signalled,
        and a vanished one is the ordinary mid-scan race.
        """
        try:
            with open(f"{_PROC}/{pid}/comm", encoding="utf-8") as f:
                return f.read().strip() != _BWRAP_COMM
        except (OSError, UnicodeDecodeError):
            return False

    @staticmethod
    def _signal(pid: int, sig: signal.Signals) -> bool:
        """Send *sig* to *pid*, returning whether the kernel accepted it.

        False on every failure — the process already exited
        (``ProcessLookupError``) or belongs to another user
        (``PermissionError``); both are subclasses of ``OSError`` and neither is
        actionable here.
        """
        try:
            os.kill(pid, sig)
        except OSError:
            return False
        return True
