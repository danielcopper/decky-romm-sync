"""AdoptionMoveAdapter — the move, and what it is allowed to leave behind.

Everything here runs against a real filesystem under ``tmp_path``, because the
whole point of the adapter is which syscall did what: a hardlink that already
exists, an ``EXDEV`` that no in-memory fake produces, a rollback that cannot put
a file back. The assertions are on the tree afterwards, never on which method was
called.

The failure injections replace one ``os`` function for the duration of one test.
That is the only way to reach an ``EXDEV`` or a mid-set ``EPERM`` without two
real filesystems and a read-only mount, and each patch is scoped to the exact
call it has to break so the rest of the adapter runs for real.
"""

from __future__ import annotations

import errno
import os
from typing import TYPE_CHECKING

import pytest

from adapters.adoption_move import AdoptionMoveAdapter

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def adapter() -> AdoptionMoveAdapter:
    return AdoptionMoveAdapter()


def _write(path: Path, data: bytes = b"payload") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _pairs(*pairs: tuple[Path, Path]) -> tuple[tuple[str, str], ...]:
    return tuple((str(source), str(target)) for source, target in pairs)


class TestListNames:
    def test_a_missing_directory_lists_nothing(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        assert adapter.list_names(str(tmp_path / "nope")) == ()

    def test_only_files_directly_inside_are_listed(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        _write(tmp_path / "a.srm")
        _write(tmp_path / "b.state")
        _write(tmp_path / "nested" / "c.srm")
        assert sorted(adapter.list_names(str(tmp_path))) == ["a.srm", "b.state"]


class TestExists:
    def test_a_dangling_symlink_still_occupies_the_name(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        link = tmp_path / "broken.srm"
        link.symlink_to(tmp_path / "gone.srm")
        assert not os.path.exists(link)
        assert adapter.exists(str(link)) is True

    def test_a_free_name_is_free(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        assert adapter.exists(str(tmp_path / "nothing")) is False


class TestIsFile:
    """What the save-backup funnel can act on — the collision guard's whole basis.

    ``exists`` and ``is_file`` disagree on exactly the entries the guard refuses,
    so the pair is what makes "taken, but not by something we can set aside"
    expressible. Symlinks are the half the in-memory fake cannot model, which is
    why they are pinned here against a real filesystem.
    """

    def test_a_regular_file_is_one(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        assert adapter.is_file(str(_write(tmp_path / "Game.srm"))) is True

    def test_a_directory_is_not(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        folder = tmp_path / "Game.srm"
        folder.mkdir()
        assert adapter.exists(str(folder)) is True
        assert adapter.is_file(str(folder)) is False

    def test_a_dangling_symlink_is_not(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        link = tmp_path / "Game.srm"
        link.symlink_to(tmp_path / "gone.srm")
        assert adapter.exists(str(link)) is True
        assert adapter.is_file(str(link)) is False

    def test_a_symlink_to_a_regular_file_is_one(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        # Followed, not refused: the funnel can move it, and refusing every link
        # would turn a working layout into an unusable one.
        _write(tmp_path / "real.srm")
        link = tmp_path / "Game.srm"
        link.symlink_to(tmp_path / "real.srm")
        assert adapter.is_file(str(link)) is True

    def test_a_free_name_is_not_a_file(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        assert adapter.is_file(str(tmp_path / "nothing")) is False


class TestMovePairsHappyPath:
    def test_an_empty_plan_does_nothing_and_says_so(self, adapter: AdoptionMoveAdapter) -> None:
        assert adapter.move_pairs(()) == {"moved": [], "stranded": [], "unmoved": [], "error": ""}

    def test_every_file_arrives_and_no_old_name_survives(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        rom = _write(tmp_path / "roms" / "Game (U).gba", b"rom bytes")
        save = _write(tmp_path / "saves" / "Game (U).srm", b"save bytes")
        targets = (tmp_path / "roms" / "Game (USA).gba", tmp_path / "saves" / "Game (USA).srm")

        outcome = adapter.move_pairs(_pairs((rom, targets[0]), (save, targets[1])))

        assert outcome["unmoved"] == []
        assert outcome["stranded"] == []
        assert outcome["error"] == ""
        assert sorted(outcome["moved"]) == sorted(str(target) for target in targets)
        assert targets[0].read_bytes() == b"rom bytes"
        assert targets[1].read_bytes() == b"save bytes"
        assert not rom.exists()
        assert not save.exists()

    def test_a_target_directory_that_does_not_exist_yet_is_created(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path
    ) -> None:
        # A content-sorted save folder is named after the ROM directory, so the
        # new name's folder has never existed before the rename.
        save = _write(tmp_path / "saves" / "Game (U)" / "disc.srm", b"save bytes")
        target = tmp_path / "saves" / "Game (USA)" / "disc.srm"

        outcome = adapter.move_pairs(_pairs((save, target)))

        assert outcome["unmoved"] == []
        assert target.read_bytes() == b"save bytes"

    def test_a_directory_source_moves_whole(self, adapter: AdoptionMoveAdapter, tmp_path: Path) -> None:
        # Hardlinks cannot name a directory, so this is the rename path — the
        # subtree has to arrive intact all the same.
        _write(tmp_path / "roms" / "Game (U)" / "inner" / "disc.bin", b"disc")
        source = tmp_path / "roms" / "Game (U)"
        target = tmp_path / "roms" / "Game (USA)"

        outcome = adapter.move_pairs(_pairs((source, target)))

        assert outcome["unmoved"] == []
        assert (target / "inner" / "disc.bin").read_bytes() == b"disc"
        assert not source.exists()


class TestMovePairsStagingFails:
    def test_a_link_failure_leaves_every_original_exactly_where_it_was(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rom = _write(tmp_path / "Game (U).gba", b"rom bytes")
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        rom_target = tmp_path / "Game (USA).gba"
        save_target = tmp_path / "Game (USA).srm"
        real_link = os.link

        def fail_on_the_save(source: str, target: str) -> None:
            if source == str(save):
                raise OSError(errno.ENOSPC, "No space left on device")
            real_link(source, target)

        monkeypatch.setattr(os, "link", fail_on_the_save)
        outcome = adapter.move_pairs(_pairs((rom, rom_target), (save, save_target)))

        assert outcome["moved"] == []
        assert sorted(outcome["unmoved"]) == sorted([str(rom), str(save)])
        assert outcome["error"]
        # The originals are untouched and the link staged before the failure was
        # taken back, so a re-run starts from exactly the same state.
        assert rom.read_bytes() == b"rom bytes"
        assert save.read_bytes() == b"save bytes"
        assert not rom_target.exists()
        assert not save_target.exists()

    def test_a_staged_link_that_cannot_be_taken_back_is_named(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rom = _write(tmp_path / "Game (U).gba", b"rom bytes")
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        rom_target = tmp_path / "Game (USA).gba"
        real_link = os.link

        def fail_on_the_save(source: str, target: str) -> None:
            if source == str(save):
                raise OSError(errno.ENOSPC, "No space left on device")
            real_link(source, target)

        monkeypatch.setattr(os, "link", fail_on_the_save)
        monkeypatch.setattr(os, "unlink", _raising_unlink(str(rom_target)))
        outcome = adapter.move_pairs(_pairs((rom, rom_target), (save, tmp_path / "Game (USA).srm")))

        assert outcome["moved"] == []
        assert "Game (USA).gba" in outcome["error"]
        assert rom.read_bytes() == b"rom bytes"


class TestMovePairsCommitFails:
    def test_an_unlink_failure_leaves_both_names_and_is_not_a_failure(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rom = _write(tmp_path / "Game (U).gba", b"rom bytes")
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        rom_target = tmp_path / "Game (USA).gba"
        save_target = tmp_path / "Game (USA).srm"
        monkeypatch.setattr(os, "unlink", _raising_unlink(str(save)))

        outcome = adapter.move_pairs(_pairs((rom, rom_target), (save, save_target)))

        # Everything the adoption needs is at its new name; one inode simply has
        # two names, which loses nothing and a re-run finishes.
        assert outcome["unmoved"] == []
        assert outcome["moved"] == [str(rom_target)]
        assert outcome["stranded"] == [str(save)]
        assert outcome["error"]
        assert rom_target.read_bytes() == b"rom bytes"
        assert save_target.read_bytes() == b"save bytes"
        assert save.read_bytes() == b"save bytes"

    def test_running_the_same_move_again_never_touches_the_arrived_content(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The state a failed unlink leaves is one the user can arrive at again by
        # pressing the same button. Re-staging a link onto an occupied target
        # fails with EEXIST, which is not a "hardlinks unsupported" errno, so the
        # set is reported unmoved rather than falling through to a rename that
        # would replace correct content with itself.
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        save_target = tmp_path / "Game (USA).srm"
        monkeypatch.setattr(os, "unlink", _raising_unlink(str(save)))
        adapter.move_pairs(_pairs((save, save_target)))
        monkeypatch.undo()

        second = adapter.move_pairs(_pairs((save, save_target)))

        assert second["unmoved"] == [str(save)]
        assert second["error"]
        assert save_target.read_bytes() == b"save bytes"
        assert save.read_bytes() == b"save bytes"


class TestMovePairsCrossDevice:
    def test_exdev_falls_back_to_rename_and_still_moves_everything(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ROMs on an SD card, saves on internal storage: ``os.link`` cannot span
        # the two, and the whole set has to arrive anyway.
        rom = _write(tmp_path / "Game (U).gba", b"rom bytes")
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        rom_target = tmp_path / "Game (USA).gba"
        save_target = tmp_path / "Game (USA).srm"
        monkeypatch.setattr(os, "link", _raising_link(errno.EXDEV))

        outcome = adapter.move_pairs(_pairs((rom, rom_target), (save, save_target)))

        assert outcome["unmoved"] == []
        assert outcome["error"] == ""
        assert rom_target.read_bytes() == b"rom bytes"
        assert save_target.read_bytes() == b"save bytes"

    def test_a_filesystem_that_refuses_hardlinks_falls_back_too(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        target = tmp_path / "Game (USA).srm"
        monkeypatch.setattr(os, "link", _raising_link(errno.EPERM))

        outcome = adapter.move_pairs(_pairs((save, target)))

        assert outcome["unmoved"] == []
        assert target.read_bytes() == b"save bytes"

    def test_the_fallback_rolls_every_completed_rename_back(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rom = _write(tmp_path / "Game (U).gba", b"rom bytes")
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        rom_target = tmp_path / "Game (USA).gba"
        save_target = tmp_path / "Game (USA).srm"
        monkeypatch.setattr(os, "link", _raising_link(errno.EXDEV))
        monkeypatch.setattr(os, "rename", _rename_failing_on(str(save)))

        outcome = adapter.move_pairs(_pairs((rom, rom_target), (save, save_target)))

        assert outcome["moved"] == []
        assert sorted(outcome["unmoved"]) == sorted([str(rom), str(save)])
        assert outcome["error"]
        assert rom.read_bytes() == b"rom bytes"
        assert save.read_bytes() == b"save bytes"
        assert not rom_target.exists()
        assert not save_target.exists()

    def test_a_rollback_that_fails_reports_the_partial_state_by_name(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rom = _write(tmp_path / "Game (U).gba", b"rom bytes")
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        rom_target = tmp_path / "Game (USA).gba"
        save_target = tmp_path / "Game (USA).srm"
        monkeypatch.setattr(os, "link", _raising_link(errno.EXDEV))
        monkeypatch.setattr(os, "rename", _rename_failing_on(str(save), str(rom_target)))

        outcome = adapter.move_pairs(_pairs((rom, rom_target), (save, save_target)))

        # Never success, never a plain failure: the ROM is at its new name, the
        # save is not, and both are named so the user can act on it.
        assert outcome["moved"] == [str(rom_target)]
        assert outcome["unmoved"] == [str(save)]
        assert "Game (USA).gba" in outcome["error"]
        assert rom_target.read_bytes() == b"rom bytes"
        assert save.read_bytes() == b"save bytes"

    def test_the_fallback_refuses_an_occupied_target_instead_of_replacing_it(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``os.rename`` replaces silently and Python exposes no RENAME_NOREPLACE,
        # so the adapter probes. A file that arrived after the plan was made must
        # not be destroyed by a move nobody asked about.
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        target = _write(tmp_path / "Game (USA).srm", b"someone else's save")
        monkeypatch.setattr(os, "link", _raising_link(errno.EXDEV))

        outcome = adapter.move_pairs(_pairs((save, target)))

        assert outcome["unmoved"] == [str(save)]
        assert target.read_bytes() == b"someone else's save"
        assert save.read_bytes() == b"save bytes"

    def test_a_link_the_undo_could_not_take_back_is_named_by_the_fallback(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A staged link that survives the undo occupies a target the rename pass
        # then needs. Without naming it the refusal reads as "something is
        # already there" about a file the plugin itself put down.
        rom = _write(tmp_path / "Game (U).gba", b"rom bytes")
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        rom_target = tmp_path / "Game (USA).gba"
        real_link = os.link

        def cross_device_on_the_save(source: str, target: str) -> None:
            if source == str(save):
                raise OSError(errno.EXDEV, "Invalid cross-device link")
            real_link(source, target)

        monkeypatch.setattr(os, "link", cross_device_on_the_save)
        monkeypatch.setattr(os, "unlink", _raising_unlink(str(rom_target)))
        outcome = adapter.move_pairs(_pairs((rom, rom_target), (save, tmp_path / "Game (USA).srm")))

        assert outcome["moved"] == []
        assert sorted(outcome["unmoved"]) == sorted([str(rom), str(save)])
        assert "left behind" in outcome["error"]
        assert "Game (USA).gba" in outcome["error"]
        assert rom.read_bytes() == b"rom bytes"
        assert save.read_bytes() == b"save bytes"

    def test_a_target_parent_that_cannot_be_created_moves_nothing(
        self, adapter: AdoptionMoveAdapter, tmp_path: Path
    ) -> None:
        save = _write(tmp_path / "Game (U).srm", b"save bytes")
        blocker = _write(tmp_path / "blocked", b"a file, not a folder")

        outcome = adapter.move_pairs(_pairs((save, blocker / "Game (USA).srm")))

        assert outcome["unmoved"] == [str(save)]
        assert outcome["error"]
        assert save.read_bytes() == b"save bytes"


def _raising_link(code: int):
    """An ``os.link`` that always fails with *code*."""

    def link(source: str, target: str) -> None:
        raise OSError(code, os.strerror(code))

    return link


def _raising_unlink(failing: str):
    """An ``os.unlink`` that fails for *failing* and works for everything else."""
    real = os.unlink

    def unlink(path: str) -> None:
        if str(path) == failing:
            raise OSError(errno.EACCES, "Permission denied")
        real(path)

    return unlink


def _rename_failing_on(*failing: str):
    """An ``os.rename`` that fails whenever *source* is one of *failing*."""
    real = os.rename

    def rename(source: str, target: str) -> None:
        if str(source) in failing:
            raise OSError(errno.EACCES, "Permission denied")
        real(source, target)

    return rename
