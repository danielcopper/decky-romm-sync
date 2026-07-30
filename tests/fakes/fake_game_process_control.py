"""In-memory ``GameProcessControl`` implementation for service tests."""

from __future__ import annotations


class FakeGameProcessControlAdapter:
    """In-memory ``GameProcessControl`` for tests.

    Models a process table without touching ``/proc``. ``pids`` is what
    :meth:`find_game_pids` reports for a matching app id (empty models "nothing
    running"); ``alive`` is the set of pids that still answer :meth:`is_alive`;
    ``survive_stop`` marks pids that stay alive through the stop request so the
    escalation to :meth:`force_kill` is exercised.

    The two signal-failure switches are deliberately separate, because a test
    that wants a failing force kill must still get a *successful* stop request
    (a pid whose stop request fails never reaches the force rung at all):

    - ``unsignalable`` — :meth:`request_stop` AND :meth:`force_kill` both fail
      (the process vanished before the ladder reached it, or is foreign).
    - ``unkillable`` — only :meth:`force_kill` fails (the process survived the
      stop request, then exited or became unreachable during the grace window).

    Every call is recorded so a test can assert the exact ladder: ``stop_calls``
    is append-only across the fake's whole lifetime, so a duplicate stop request
    for one pid — whether from a retry loop or from a second concurrent call —
    is directly observable. That is the guard for the never-re-request
    save-safety invariant.
    """

    def __init__(self, pids: list[int] | None = None, app_id: str | None = None) -> None:
        self.pids: list[int] = list(pids) if pids else []
        # When set, only this app id resolves to ``pids``; any other reports none.
        self.app_id = app_id
        self.alive: set[int] = set(self.pids)
        self.unsignalable: set[int] = set()
        self.unkillable: set[int] = set()
        self.survive_stop: set[int] = set()
        self.find_calls: list[str] = []
        self.stop_calls: list[int] = []
        self.kill_calls: list[int] = []
        self.alive_calls: list[int] = []

    def find_game_pids(self, flatpak_app_id: str) -> list[int]:
        self.find_calls.append(flatpak_app_id)
        if self.app_id is not None and flatpak_app_id != self.app_id:
            return []
        return list(self.pids)

    def request_stop(self, pid: int) -> bool:
        self.stop_calls.append(pid)
        if pid in self.unsignalable:
            return False
        if pid not in self.survive_stop:
            self.alive.discard(pid)
        return True

    def force_kill(self, pid: int) -> bool:
        self.kill_calls.append(pid)
        if pid in self.unsignalable or pid in self.unkillable:
            return False
        self.alive.discard(pid)
        return True

    def is_alive(self, pid: int) -> bool:
        self.alive_calls.append(pid)
        return pid in self.alive
