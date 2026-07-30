"""Unit tests for ``GameProcessAdapter`` — the flatpak-instance + ``/proc`` reader.

Drives a fake flatpak instance registry and a fake ``/proc`` tree under
``tmp_path`` by pointing the adapter's ``_RUNTIME_BASE`` / ``_PROC`` module
constants at them, so instance resolution, the deepest-first descent, and every
fail-soft path are exercised without a live sandbox. The signal methods are
driven through a stubbed ``os.kill`` — nothing here ever signals a real process.
"""

from __future__ import annotations

import json
import os
import signal
from typing import TYPE_CHECKING

import pytest

from adapters import game_process
from adapters.game_process import GameProcessAdapter

if TYPE_CHECKING:
    from pathlib import Path

APP_ID = "net.retrodeck.retrodeck"
UID = 4242


@pytest.fixture
def proc_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``/proc`` the adapter reads process identity + children from."""
    root = tmp_path / "proc"
    root.mkdir()
    monkeypatch.setattr(game_process, "_PROC", str(root))
    return root


@pytest.fixture
def instances_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A fake ``/run/user/<uid>/.flatpak`` the adapter resolves instances from."""
    monkeypatch.setattr(game_process, "_RUNTIME_BASE", str(tmp_path / "run" / "user"))
    monkeypatch.setattr(game_process.os, "getuid", lambda: UID)
    root = tmp_path / "run" / "user" / str(UID) / ".flatpak"
    root.mkdir(parents=True)
    return root


def _make_instance(
    instances_root: Path,
    instance_id: str,
    *,
    app_id: str | None = APP_ID,
    child_pid: object = 1000,
    bwrapinfo: str | None = None,
) -> None:
    """Write one fake flatpak instance dir (``info`` + ``bwrapinfo.json``).

    *app_id* ``None`` omits the ``name=`` line entirely; *bwrapinfo* overrides
    the JSON body verbatim (for the corrupt-file cases); *child_pid* ``None``
    omits the ``child-pid`` key.
    """
    entry = instances_root / instance_id
    entry.mkdir()
    info = "[Application]\nruntime=runtime/org.kde.Platform/x86_64/6.7\n"
    if app_id is not None:
        info += f"name={app_id}\n"
    info += "[Instance]\ninstance-id=" + instance_id + "\n"
    (entry / "info").write_text(info, encoding="utf-8")
    if bwrapinfo is not None:
        (entry / "bwrapinfo.json").write_text(bwrapinfo, encoding="utf-8")
    elif child_pid is not None:
        (entry / "bwrapinfo.json").write_text(json.dumps({"child-pid": child_pid}), encoding="utf-8")


def _make_process(proc_root: Path, pid: int, comm: str, children: list[int] | None = None) -> None:
    """Write a fake ``/proc/<pid>`` entry with a ``comm`` and a ``children`` list."""
    entry = proc_root / str(pid)
    task = entry / "task" / str(pid)
    task.mkdir(parents=True)
    (entry / "comm").write_text(f"{comm}\n", encoding="utf-8")
    task.joinpath("children").write_text(" ".join(str(c) for c in (children or [])) + " \n", encoding="utf-8")


def _make_status(proc_root: Path, pid: int, state: str) -> None:
    """Write a fake ``/proc/<pid>/status`` whose ``State:`` line carries *state*."""
    entry = proc_root / str(pid)
    entry.mkdir(exist_ok=True)
    (entry / "status").write_text(
        f"Name:\tretroarch\nUmask:\t0022\nState:\t{state} (whatever)\nTgid:\t{pid}\n",
        encoding="utf-8",
    )


# ── find_game_pids ───────────────────────────────────────────────────────────


class TestFindGamePids:
    def test_returns_the_instance_subtree_deepest_first_without_bwrap(
        self, instances_root: Path, proc_root: Path
    ) -> None:
        # bwrap(1000) → sh(1001) → retroarch(1002) → audio helper(1003).
        _make_instance(instances_root, "inst-a", child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        _make_process(proc_root, 1001, "sh", [1002])
        _make_process(proc_root, 1002, "retroarch", [1003])
        _make_process(proc_root, 1003, "pipewire", [])

        # Deepest first, and the bwrap scaffolding never appears as a target.
        assert GameProcessAdapter().find_game_pids(APP_ID) == [1003, 1002, 1001]

    def test_sibling_children_share_a_level_and_precede_their_parent(
        self, instances_root: Path, proc_root: Path
    ) -> None:
        _make_instance(instances_root, "inst-a", child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        _make_process(proc_root, 1001, "retrodeck.sh", [1002, 1003])
        _make_process(proc_root, 1002, "duckstation", [])
        _make_process(proc_root, 1003, "helper", [])

        pids = GameProcessAdapter().find_game_pids(APP_ID)
        assert pids[-1] == 1001
        assert set(pids[:2]) == {1002, 1003}

    def test_nested_bwrap_is_descended_through_but_never_returned(self, instances_root: Path, proc_root: Path) -> None:
        # flatpak nests an inner bwrap; only the emulator below it is a target.
        _make_instance(instances_root, "inst-a", child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        _make_process(proc_root, 1001, "bwrap", [1002])
        _make_process(proc_root, 1002, "pcsx2", [])

        assert GameProcessAdapter().find_game_pids(APP_ID) == [1002]

    def test_two_live_instances_of_the_same_app_are_both_walked_and_deduped(
        self, instances_root: Path, proc_root: Path
    ) -> None:
        _make_instance(instances_root, "inst-a", child_pid=1000)
        _make_instance(instances_root, "inst-b", child_pid=2000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        _make_process(proc_root, 1001, "retroarch", [])
        _make_process(proc_root, 2000, "bwrap", [2001])
        _make_process(proc_root, 2001, "dolphin-emu", [])

        assert sorted(GameProcessAdapter().find_game_pids(APP_ID)) == [1001, 2001]

    def test_a_different_app_id_resolves_to_nothing(self, instances_root: Path, proc_root: Path) -> None:
        _make_instance(instances_root, "inst-a", app_id="org.videolan.VLC", child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        _make_process(proc_root, 1001, "vlc", [])

        assert GameProcessAdapter().find_game_pids(APP_ID) == []

    def test_an_app_id_that_is_only_a_prefix_of_ours_does_not_match(
        self, instances_root: Path, proc_root: Path
    ) -> None:
        # Whole-line matching: "name=net.retrodeck.retrodeck.Debug" is a different app.
        _make_instance(instances_root, "inst-a", app_id=f"{APP_ID}.Debug", child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        _make_process(proc_root, 1001, "retroarch", [])

        assert GameProcessAdapter().find_game_pids(APP_ID) == []

    def test_an_info_file_without_a_name_line_is_skipped(self, instances_root: Path, proc_root: Path) -> None:
        _make_instance(instances_root, "inst-a", app_id=None, child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        _make_process(proc_root, 1001, "retroarch", [])

        assert GameProcessAdapter().find_game_pids(APP_ID) == []

    def test_an_instance_without_an_info_file_is_skipped(self, instances_root: Path, proc_root: Path) -> None:
        (instances_root / "inst-a").mkdir()

        assert GameProcessAdapter().find_game_pids(APP_ID) == []

    def test_a_missing_bwrapinfo_is_skipped_without_failing_the_scan(
        self, instances_root: Path, proc_root: Path
    ) -> None:
        # The dying instance has no bwrapinfo; the live one still resolves.
        _make_instance(instances_root, "inst-dead", child_pid=None)
        _make_instance(instances_root, "inst-live", child_pid=2000)
        _make_process(proc_root, 2000, "bwrap", [2001])
        _make_process(proc_root, 2001, "retroarch", [])

        assert GameProcessAdapter().find_game_pids(APP_ID) == [2001]

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param("{not json at all", id="unparseable"),
            pytest.param("", id="empty-file"),
            pytest.param("[1, 2, 3]", id="json-but-not-an-object"),
            pytest.param('{"pid": 1000}', id="no-child-pid-key"),
            pytest.param('{"child-pid": "1000"}', id="child-pid-is-a-string"),
            pytest.param('{"child-pid": true}', id="child-pid-is-a-bool"),
            pytest.param('{"child-pid": 0}', id="child-pid-is-zero"),
            pytest.param('{"child-pid": -5}', id="child-pid-is-negative"),
            pytest.param('{"child-pid": null}', id="child-pid-is-null"),
        ],
    )
    def test_an_unusable_bwrapinfo_yields_no_pids(self, instances_root: Path, proc_root: Path, body: str) -> None:
        _make_instance(instances_root, "inst-a", bwrapinfo=body)

        assert GameProcessAdapter().find_game_pids(APP_ID) == []

    def test_a_pid_that_vanished_mid_scan_is_skipped_not_fatal(self, instances_root: Path, proc_root: Path) -> None:
        # 1002 is listed as a child but its /proc entry is already gone (it
        # exited between the parent's children read and the comm read).
        _make_instance(instances_root, "inst-a", child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        _make_process(proc_root, 1001, "retroarch", [1002])

        assert GameProcessAdapter().find_game_pids(APP_ID) == [1001]

    def test_a_root_pid_that_vanished_before_the_walk_yields_no_pids(
        self, instances_root: Path, proc_root: Path
    ) -> None:
        # The instance dir still exists but its bwrap is already reaped.
        _make_instance(instances_root, "inst-a", child_pid=1000)

        assert GameProcessAdapter().find_game_pids(APP_ID) == []

    def test_an_absent_instance_registry_yields_no_pids(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, proc_root: Path
    ) -> None:
        # No flatpak has ever run for this uid — the .flatpak dir does not exist.
        monkeypatch.setattr(game_process, "_RUNTIME_BASE", str(tmp_path / "nowhere"))
        monkeypatch.setattr(game_process.os, "getuid", lambda: UID)

        assert GameProcessAdapter().find_game_pids(APP_ID) == []

    def test_an_unreadable_instance_registry_yields_no_pids(
        self, instances_root: Path, proc_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _make_instance(instances_root, "inst-a", child_pid=1000)

        def _boom(_path: str) -> list[str]:
            raise PermissionError("nope")

        monkeypatch.setattr(game_process.os, "listdir", _boom)

        assert GameProcessAdapter().find_game_pids(APP_ID) == []

    def test_a_self_referencing_children_list_terminates(self, instances_root: Path, proc_root: Path) -> None:
        # /proc can't really produce a cycle, but the visited set must make one
        # harmless rather than hanging the backend.
        _make_instance(instances_root, "inst-a", child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        _make_process(proc_root, 1001, "retroarch", [1000, 1001])

        assert GameProcessAdapter().find_game_pids(APP_ID) == [1001]

    def test_a_children_file_with_junk_tokens_keeps_only_the_pids(self, instances_root: Path, proc_root: Path) -> None:
        _make_instance(instances_root, "inst-a", child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [])
        (proc_root / "1000" / "task" / "1000" / "children").write_text("1001 -3 abc \n", encoding="utf-8")
        _make_process(proc_root, 1001, "retroarch", [])

        assert GameProcessAdapter().find_game_pids(APP_ID) == [1001]

    def test_a_tree_deeper_than_the_depth_cap_stops_at_the_cap(self, instances_root: Path, proc_root: Path) -> None:
        # A chain twice as deep as the cap: the walk is bounded, and what it did
        # reach is still ordered deepest-first.
        depth = game_process._MAX_DEPTH * 2
        _make_instance(instances_root, "inst-a", child_pid=1000)
        _make_process(proc_root, 1000, "bwrap", [1001])
        for offset in range(1, depth):
            _make_process(proc_root, 1000 + offset, f"level{offset}", [1000 + offset + 1])
        _make_process(proc_root, 1000 + depth, "deepest", [])

        pids = GameProcessAdapter().find_game_pids(APP_ID)
        assert len(pids) == game_process._MAX_DEPTH - 1
        assert pids == sorted(pids, reverse=True)


# ── request_stop / force_kill ────────────────────────────────────────────────


class TestSignalDelivery:
    @staticmethod
    def _record_kills(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
        sent: list[tuple[int, int]] = []
        monkeypatch.setattr(game_process.os, "kill", lambda pid, sig: sent.append((pid, sig)))
        return sent

    def test_request_stop_sends_exactly_one_sigterm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._record_kills(monkeypatch)

        assert GameProcessAdapter().request_stop(4321) is True
        assert sent == [(4321, signal.SIGTERM)]

    def test_force_kill_sends_sigkill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sent = self._record_kills(monkeypatch)

        assert GameProcessAdapter().force_kill(4321) is True
        assert sent == [(4321, signal.SIGKILL)]

    @pytest.mark.parametrize(
        "error",
        [
            pytest.param(ProcessLookupError(), id="already-exited"),
            pytest.param(PermissionError(), id="not-ours-to-signal"),
            pytest.param(OSError("weird"), id="other-oserror"),
        ],
    )
    def test_an_undeliverable_signal_reports_false_instead_of_raising(
        self, monkeypatch: pytest.MonkeyPatch, error: OSError
    ) -> None:
        def _boom(_pid: int, _sig: int) -> None:
            raise error

        monkeypatch.setattr(game_process.os, "kill", _boom)
        adapter = GameProcessAdapter()

        assert adapter.request_stop(4321) is False
        assert adapter.force_kill(4321) is False


# ── is_alive ─────────────────────────────────────────────────────────────────


class TestIsAlive:
    @pytest.mark.parametrize("state", ["R", "S", "D", "T", "I"])
    def test_a_running_state_reads_as_alive(self, proc_root: Path, state: str) -> None:
        _make_status(proc_root, 900, state)

        assert GameProcessAdapter().is_alive(900) is True

    @pytest.mark.parametrize("state", ["Z", "X", "x"])
    def test_an_exited_state_reads_as_not_alive(self, proc_root: Path, state: str) -> None:
        # A zombie has already run its exit path; calling it alive would burn the
        # caller's whole grace window waiting for something that never changes.
        _make_status(proc_root, 900, state)

        assert GameProcessAdapter().is_alive(900) is False

    def test_a_missing_status_reads_as_not_alive(self, proc_root: Path) -> None:
        assert GameProcessAdapter().is_alive(900) is False

    def test_a_status_without_a_state_line_reads_as_not_alive(self, proc_root: Path) -> None:
        entry = proc_root / "900"
        entry.mkdir()
        (entry / "status").write_text("Name:\tretroarch\nTgid:\t900\n", encoding="utf-8")

        assert GameProcessAdapter().is_alive(900) is False

    def test_a_truncated_state_line_reads_as_not_alive(self, proc_root: Path) -> None:
        # A mid-write read can leave "State:" with no value — the IndexError guard.
        entry = proc_root / "900"
        entry.mkdir()
        (entry / "status").write_text("Name:\tretroarch\nState:\n", encoding="utf-8")

        assert GameProcessAdapter().is_alive(900) is False


def test_the_instance_registry_path_is_built_from_the_real_uid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The runtime dir is derived from ``os.getuid()``, never a hardcoded 1000."""
    monkeypatch.setattr(game_process, "_RUNTIME_BASE", str(tmp_path / "run" / "user"))
    monkeypatch.setattr(game_process.os, "getuid", lambda: 1337)
    listed: list[str] = []

    def _record(path: str) -> list[str]:
        listed.append(path)
        return []

    monkeypatch.setattr(game_process.os, "listdir", _record)

    GameProcessAdapter().find_game_pids(APP_ID)

    assert listed == [os.path.join(str(tmp_path / "run" / "user"), "1337", ".flatpak")]
