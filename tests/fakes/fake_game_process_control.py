"""In-memory ``GameProcessControl`` implementation for service tests."""

from __future__ import annotations

from domain.game_instance import GameInstance

# The launch path the single-instance shorthand puts on its command line. Tests
# that only care about the ladder point their launch-path seam at this, so the
# instance matches and the ladder runs; tests that care about the MATCH build
# their instances explicitly with ``add_instance``.
DEFAULT_LAUNCH_PATH = "/home/deck/retrodeck/roms/gba/game.gba"


class FakeGameProcessControlAdapter:
    """In-memory ``GameProcessControl`` for tests.

    Models a process table without touching ``/proc``. The table is a list of
    live :class:`GameInstance` trees; :attr:`pids` is the single-instance
    shorthand (assigning it replaces the table with one instance running
    :data:`DEFAULT_LAUNCH_PATH`, and reading it flattens whatever is there), and
    :meth:`add_instance` builds the multi-instance tables the match is about.
    ``alive`` is the set of pids that still answer :meth:`is_alive`;
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
    save-safety invariant. ``kill_calls`` carries the same weight for the
    never-signal-an-unmatched-instance rule: a pid that belongs to another
    instance must appear in neither list.
    """

    def __init__(self, pids: list[int] | None = None, app_id: str | None = None) -> None:
        self.instances: list[GameInstance] = []
        # When set, only this app id resolves to the table; any other reports none.
        self.app_id = app_id
        self.alive: set[int] = set()
        self.unsignalable: set[int] = set()
        self.unkillable: set[int] = set()
        self.survive_stop: set[int] = set()
        self.find_calls: list[str] = []
        self.stop_calls: list[int] = []
        self.kill_calls: list[int] = []
        self.alive_calls: list[int] = []
        if pids:
            self.pids = list(pids)

    @property
    def pids(self) -> list[int]:
        """Every live pid across the table, in instance order."""
        return [pid for instance in self.instances for pid in instance.pids]

    @pids.setter
    def pids(self, value: list[int]) -> None:
        """Replace the table with ONE instance running :data:`DEFAULT_LAUNCH_PATH`.

        Also re-seeds ``alive`` from *value*: a fresh table describes a fresh set
        of live processes, and a test that wants a pid to read as dead assigns
        ``alive`` afterwards (as it always had to).
        """
        self.instances = [GameInstance(pids=tuple(value), argv=(DEFAULT_LAUNCH_PATH,))] if value else []
        self.alive = set(value)

    def add_instance(self, pids: list[int], launch_path: str) -> GameInstance:
        """Append one more live instance whose command line runs *launch_path*."""
        instance = GameInstance(pids=tuple(pids), argv=("/app/bin/retroarch", launch_path))
        self.instances.append(instance)
        self.alive.update(pids)
        return instance

    def find_game_instances(self, flatpak_app_id: str) -> list[GameInstance]:
        self.find_calls.append(flatpak_app_id)
        if self.app_id is not None and flatpak_app_id != self.app_id:
            return []
        return list(self.instances)

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
