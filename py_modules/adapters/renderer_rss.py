"""Renderer RSS adapter — concrete ``RendererRssFn`` reading ``/proc`` directly.

Reports the resident-set size of Steam's ``SharedJSContext`` renderer so the
session-budget gate can decide whether the next apply chunk would cross Steam's
per-session heap budget. Reads ``/proc/[pid]/comm`` + ``status`` directly (no
subprocess) and returns the **maximum** ``VmRSS`` across all ``steamwebhelper``
processes, in KB, or ``None`` on any failure (fail-open — the gate skips when the
reading is unavailable).

Max-child heuristic — why the largest ``steamwebhelper`` IS the renderer:
``steamwebhelper`` spawns many child processes (GPU, utility, several renderers)
and the ``SharedJSContext`` renderer that hosts the plugin UI is the memory-heavy
one. Filtering by cmdline ``--type=renderer`` is impossible: the renderers are
forked from a zygote and inherit the zygote's cmdline, so ``--type`` is absent
from the forked process. The max-RSS process is the discriminator instead —
validated on-device 2026-07-11: the max-RSS ``steamwebhelper`` grew 438 → 2528 MB
across a sync, crashed at the cliff, and respawned at the fresh baseline, exactly
tracking the ``SharedJSContext`` lifecycle. The heuristic only ever misidentifies
toward a *larger* reading (some other child briefly larger), which makes the gate
pause slightly early — harmless — never late.
"""

from __future__ import annotations

import os

_PROC = "/proc"
# ``/proc/[pid]/comm`` is the kernel-truncated (15-char) process name;
# "steamwebhelper" is 14 chars, so it survives truncation intact.
_TARGET_COMM = "steamwebhelper"


class RendererRssAdapter:
    """Real ``RendererRssFn`` backed by ``/proc``."""

    def __call__(self) -> int | None:
        """Return the max ``steamwebhelper`` ``VmRSS`` in KB, or ``None`` on failure.

        ``None`` covers every unavailable-measurement case: ``/proc`` unreadable,
        or no ``steamwebhelper`` process present at all. A per-process read that
        fails (the process exited between the directory scan and the read — a
        normal race) is skipped, not fatal, so one vanishing child never blanks
        the whole reading.
        """
        try:
            pids = os.listdir(_PROC)
        except OSError:
            return None
        max_rss: int | None = None
        for entry in pids:
            if not entry.isdigit():
                continue
            rss = self._read_rss_kb(entry)
            if rss is not None and (max_rss is None or rss > max_rss):
                max_rss = rss
        return max_rss

    @staticmethod
    def _read_rss_kb(pid: str) -> int | None:
        """Return *pid*'s ``VmRSS`` in KB if it is a ``steamwebhelper``, else ``None``.

        Returns ``None`` for a non-matching ``comm``, a missing ``VmRSS`` line, or
        any read failure (the process may exit mid-read — a benign race).
        """
        try:
            with open(f"{_PROC}/{pid}/comm", encoding="utf-8") as f:
                if f.read().strip() != _TARGET_COMM:
                    return None
            with open(f"{_PROC}/{pid}/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        # Line shape: "VmRSS:\t   1234567 kB" — the second field is KB.
                        return int(line.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        return None
