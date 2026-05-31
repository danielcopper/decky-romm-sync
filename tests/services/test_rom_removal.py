"""Tests for RomRemovalService — ROM file deletion and ``rom_installs`` cleanup."""

import asyncio
import logging
import os
import sys

import pytest
from fakes.fake_download_queue_cleanup import FakeDownloadQueueCleanup
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_rom_file_store import FakeRomFileStore
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))
sys.path.insert(0, os.path.dirname(__file__))

# conftest.py patches decky before this import
from domain.rom import Rom
from domain.rom_install import RomInstall
from services.rom_removal import RomRemovalService, RomRemovalServiceConfig

# Synthetic roms-base path used by the fake fs throughout this module.
_ROMS_BASE = "/retrodeck/roms"


@pytest.fixture
def logger():
    return logging.getLogger("test_rom_removal")


@pytest.fixture
def queue_cleanup() -> FakeDownloadQueueCleanup:
    return FakeDownloadQueueCleanup()


@pytest.fixture
def rom_files() -> FakeRomFileStore:
    return FakeRomFileStore()


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def service(logger, queue_cleanup, rom_files, uow):
    return RomRemovalService(
        config=RomRemovalServiceConfig(
            logger=logger,
            loop=asyncio.new_event_loop(),
            rom_file_store=rom_files,
            retrodeck_paths=FakeRetroDeckPaths(roms=_ROMS_BASE),
            download_queue_cleanup=queue_cleanup,
            uow_factory=FakeUnitOfWorkFactory(uow),
        ),
    )


@pytest.fixture(autouse=True)
async def _sync_loop(service):
    """Keep service loop in sync with the running event loop."""
    service._loop = asyncio.get_event_loop()


def _make_rom(rom_id: int, *, platform_slug: str = "n64") -> Rom:
    """Build the FK-parent ``roms`` row so a child ``rom_installs`` write commits."""
    return Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.z64",
        shortcut_app_id=1000 + rom_id,
        last_synced_at="2025-01-01T00:00:00",
    )


def _make_install(rom_id: int, *, file_path: str, rom_dir: str | None = None, system: str = "n64") -> RomInstall:
    return RomInstall.mark_installed(
        rom_id=rom_id,
        file_path=file_path,
        rom_dir=rom_dir,
        platform_slug=system,
        system=system,
        installed_at="2025-01-01T00:00:00",
    )


def _seed_install(uow: FakeUnitOfWork, install: RomInstall, *, platform_slug: str = "n64") -> None:
    """Seed the FK-parent Rom THEN its install record, in one commit."""
    with uow:
        uow.roms.save(_make_rom(install.rom_id, platform_slug=platform_slug))
        uow.rom_installs.save(install)


class TestDeleteRomFiles:
    def test_deletes_single_file(self, service, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"\x00" * 100

        service._delete_rom_files(_make_install(1, file_path=rom_path))

        assert rom_path not in rom_files.files
        assert rom_files.remove_file_calls == [rom_path]
        # A single-file ROM has no rom_dir, so no directory tree is ever removed.
        assert rom_files.remove_tree_calls == []

    def test_deletes_rom_dir(self, service, rom_files):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.cue"] = b"cue"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100

        service._delete_rom_files(_make_install(1, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"))

        assert f"{rom_dir}/disc1.cue" not in rom_files.files
        assert f"{rom_dir}/disc1.bin" not in rom_files.files
        assert rom_files.remove_tree_calls == [rom_dir]

    def test_single_file_owns_no_dir_so_system_dir_not_removed(self, service, rom_files):
        """A single-file ROM (``rom_dir`` is ``None``) lives in the shared ``<roms>/<system>`` dir.

        With no ``rom_dir`` set, the directory tree is never removed — only the
        launch file is deleted. Removing the shared system dir would wipe the
        whole platform's folder.
        """
        system_dir = f"{_ROMS_BASE}/n64"
        rom_path = f"{system_dir}/game.z64"
        sibling = f"{system_dir}/other_game.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        rom_files.files[sibling] = b"\x00" * 100
        rom_files.dirs.add(system_dir)

        service._delete_rom_files(_make_install(1, file_path=rom_path, rom_dir=None))

        assert rom_path not in rom_files.files
        assert sibling in rom_files.files  # the platform's other ROM survives
        assert system_dir in rom_files.dirs  # the system dir itself survives
        assert rom_files.remove_tree_calls == []

    def test_refuses_file_outside_roms_dir(self, service, rom_files):
        evil = "/evil/important.txt"
        rom_files.files[evil] = b"do not delete"

        service._delete_rom_files(_make_install(1, file_path=evil, rom_dir=None))

        assert evil in rom_files.files
        assert rom_files.remove_file_calls == []
        assert rom_files.remove_tree_calls == []

    def test_refuses_rom_dir_outside_roms_dir(self, service, rom_files):
        evil_dir = "/evil/dir"
        rom_files.files[f"{evil_dir}/file.txt"] = b"important"

        service._delete_rom_files(_make_install(1, file_path="", rom_dir=evil_dir))

        assert f"{evil_dir}/file.txt" in rom_files.files
        assert rom_files.remove_tree_calls == []

    def test_missing_file_no_crash(self, service):
        # File doesn't exist — should not raise and should not call any I/O
        service._delete_rom_files(_make_install(1, file_path=f"{_ROMS_BASE}/n64/gone.z64"))

    def test_empty_paths_no_crash(self, service):
        # No file_path, no rom_dir
        service._delete_rom_files(_make_install(1, file_path="", rom_dir=None))


class TestRemoveRom:
    @pytest.mark.asyncio
    async def test_removes_file_and_clears_install_record(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(42, file_path=rom_path))

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert rom_path not in rom_files.files
        assert uow.rom_installs.get(42) is None
        assert uow.committed is True

    @pytest.mark.asyncio
    async def test_returns_error_if_not_installed(self, service):
        result = await service.remove_rom(999)
        assert result["success"] is False
        assert "not installed" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_accepts_string_rom_id(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(7, file_path=rom_path))

        result = await service.remove_rom("7")

        assert result["success"] is True
        assert uow.rom_installs.get(7) is None

    @pytest.mark.asyncio
    async def test_file_already_gone_still_deletes_record(self, service, uow):
        """Edge: the file is already gone on disk → the install record is still dropped."""
        _seed_install(
            uow,
            _make_install(42, file_path=f"{_ROMS_BASE}/n64/gone.z64"),
        )

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert uow.rom_installs.get(42) is None

    @pytest.mark.asyncio
    async def test_retains_playtime_saves_and_roms_row(self, service, uow, rom_files):
        """RETENTION (ADR-0007 / D1): uninstall drops only files + the install record.

        Playtime, the save-sync state, and the ``roms`` identity row all survive.
        """
        from domain.playtime import Playtime
        from domain.rom_save_state import RomSaveState

        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100

        playtime = Playtime(total_seconds=3600, session_count=2)
        save_state = RomSaveState(active_slot="default", slot_confirmed=True)
        with uow:
            uow.roms.save(_make_rom(42))
            uow.rom_installs.save(_make_install(42, file_path=rom_path))
            uow.playtime.save(42, playtime)
            uow.rom_save_states.save(42, save_state)

        result = await service.remove_rom(42)

        assert result["success"] is True
        # Only the install record is gone.
        assert uow.rom_installs.get(42) is None
        # Identity, playtime, and save-sync state all survive the uninstall.
        assert uow.roms.get(42) is not None
        surviving_playtime = uow.playtime.get(42)
        assert surviving_playtime is not None
        assert surviving_playtime.total_seconds == 3600
        surviving_save = uow.rom_save_states.get(42)
        assert surviving_save is not None
        assert surviving_save.active_slot == "default"
        assert uow.committed is True

    @pytest.mark.asyncio
    async def test_removes_rom_dir(self, service, uow, rom_files):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/FF7.m3u"] = b"disc1.cue"
        rom_files.files[f"{rom_dir}/disc1.cue"] = b"cue"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        # Mark the parent system dir as existing so we can assert it's preserved.
        rom_files.dirs.add(f"{_ROMS_BASE}/psx")
        _seed_install(
            uow,
            _make_install(42, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"),
            platform_slug="psx",
        )

        result = await service.remove_rom(42)

        assert result["success"] is True
        # rom_dir gone
        assert all(not p.startswith(rom_dir + "/") for p in rom_files.files)
        # Parent system dir still tracked
        assert f"{_ROMS_BASE}/psx" in rom_files.dirs

    @pytest.mark.asyncio
    async def test_path_traversal_rejected_record_still_deleted(self, service, uow, rom_files):
        evil = "/etc/passwd"
        rom_files.files[evil] = b"root:x:0:0"
        _seed_install(uow, _make_install(99, file_path=evil, rom_dir=None))

        result = await service.remove_rom(99)

        assert result["success"] is True
        assert evil in rom_files.files  # not deleted (outside roms dir)
        assert uow.rom_installs.get(99) is None

    @pytest.mark.asyncio
    async def test_removes_nested_single_file_entry(self, service, uow, rom_files):
        """Nested-single-file installs (#226): the resolved filename is in file_path; rom_dir is None (no folder)."""
        system_dir = f"{_ROMS_BASE}/dc"
        rom_path = f"{system_dir}/Resident Evil.chd"
        rom_files.files[rom_path] = b"\x00" * 100
        rom_files.dirs.add(system_dir)
        _seed_install(
            uow,
            _make_install(42, file_path=rom_path, rom_dir=None, system="dc"),
            platform_slug="dc",
        )

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert rom_path not in rom_files.files
        # Parent system dir still tracked
        assert system_dir in rom_files.dirs
        assert uow.rom_installs.get(42) is None


class TestUninstallAllRoms:
    @pytest.mark.asyncio
    async def test_removes_all_installed(self, service, uow, rom_files):
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        rom_files.files[file_a] = b"\x00" * 100
        rom_files.files[file_b] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2))
            uow.rom_installs.save(_make_install(1, file_path=file_a))
            uow.rom_installs.save(_make_install(2, file_path=file_b))

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["removed_count"] == 2
        assert file_a not in rom_files.files
        assert file_b not in rom_files.files
        assert list(uow.rom_installs.iter_all()) == []

    @pytest.mark.asyncio
    async def test_clears_records_even_if_files_missing(self, service, uow):
        _seed_install(uow, _make_install(1, file_path=f"{_ROMS_BASE}/n64/nonexistent.z64"))

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert list(uow.rom_installs.iter_all()) == []

    @pytest.mark.asyncio
    async def test_handles_empty_state(self, service, uow):
        _ = uow
        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 0

    @pytest.mark.asyncio
    async def test_retains_playtime_and_roms_rows(self, service, uow, rom_files):
        """RETENTION (ADR-0007 / D1): bulk uninstall drops only files + install records.

        Identity rows and playtime survive for every ROM.
        """
        from domain.playtime import Playtime

        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        rom_files.files[file_a] = b"\x00" * 100
        rom_files.files[file_b] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2))
            uow.rom_installs.save(_make_install(1, file_path=file_a))
            uow.rom_installs.save(_make_install(2, file_path=file_b))
            uow.playtime.save(1, Playtime(total_seconds=100, session_count=1))
            uow.playtime.save(2, Playtime(total_seconds=200, session_count=1))

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert list(uow.rom_installs.iter_all()) == []
        # Identity + playtime survive the bulk uninstall.
        assert uow.roms.get(1) is not None
        assert uow.roms.get(2) is not None
        pt1 = uow.playtime.get(1)
        pt2 = uow.playtime.get(2)
        assert pt1 is not None and pt1.total_seconds == 100
        assert pt2 is not None and pt2.total_seconds == 200
        assert uow.committed is True

    @pytest.mark.asyncio
    async def test_deletes_rom_directories(self, service, uow, rom_files):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        _seed_install(
            uow,
            _make_install(1, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"),
            platform_slug="psx",
        )

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 1
        assert all(not p.startswith(rom_dir + "/") for p in rom_files.files)

    @pytest.mark.asyncio
    async def test_outside_roms_dir_skipped_record_still_cleared(self, service, uow, rom_files):
        good_file = f"{_ROMS_BASE}/n64/game_a.z64"
        rom_files.files[good_file] = b"\x00" * 100
        bad_file = "/outside/game_b.z64"
        rom_files.files[bad_file] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2, platform_slug="snes"))
            uow.rom_installs.save(_make_install(1, file_path=good_file))
            uow.rom_installs.save(_make_install(2, file_path=bad_file, rom_dir=None, system="snes"))

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert good_file not in rom_files.files
        assert bad_file in rom_files.files  # not deleted (outside roms dir)
        # Install records for the path-rejected (no exception) ROMs are still cleared:
        # the safety guard returns silently, so the deletion is treated as "succeeded".
        assert list(uow.rom_installs.iter_all()) == []

    @pytest.mark.asyncio
    async def test_partial_failure_reports_errors_and_not_success(self, service, uow, rom_files):
        """Bad path: one of three deletions raises OSError → ``success`` is False, the failing record survives."""
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        file_c = f"{_ROMS_BASE}/n64/game_c.z64"
        for p in (file_a, file_b, file_c):
            rom_files.files[p] = b"\x00" * 100
        rom_files.remove_file_failures.add(file_b)
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2))
            uow.roms.save(_make_rom(3))
            uow.rom_installs.save(_make_install(1, file_path=file_a))
            uow.rom_installs.save(_make_install(2, file_path=file_b))
            uow.rom_installs.save(_make_install(3, file_path=file_c))

        result = await service.uninstall_all_roms()

        assert result["success"] is False
        assert result["removed_count"] == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["rom_id"] == "2"
        assert "game_b.z64" in result["errors"][0]["error"]
        # Records for successful deletions are cleared; the failing entry survives so the user can retry.
        assert uow.rom_installs.get(1) is None
        assert uow.rom_installs.get(2) is not None
        assert uow.rom_installs.get(3) is None

    @pytest.mark.asyncio
    async def test_all_success_returns_empty_errors(self, service, uow, rom_files):
        """Happy path: all 3 deletions succeed → ``success`` is True and ``errors`` is empty."""
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        file_c = f"{_ROMS_BASE}/n64/game_c.z64"
        for p in (file_a, file_b, file_c):
            rom_files.files[p] = b"\x00" * 100
        with uow:
            for rid, fp in ((1, file_a), (2, file_b), (3, file_c)):
                uow.roms.save(_make_rom(rid))
                uow.rom_installs.save(_make_install(rid, file_path=fp))

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["removed_count"] == 3
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_empty_state_returns_success_with_empty_errors(self, service, uow):
        """Edge: no installed ROMs → ``success`` is True and ``errors`` is empty."""
        _ = uow
        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["removed_count"] == 0
        assert result["errors"] == []


class TestDownloadQueueCleanup:
    """Eviction of the download queue on successful ROM removal."""

    @pytest.mark.asyncio
    async def test_remove_rom_evicts_queue_on_success(self, service, uow, queue_cleanup, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(42, file_path=rom_path))

        result = await service.remove_rom(42)
        assert result["success"] is True
        assert queue_cleanup.evicted == [42]
        assert queue_cleanup.cleared == 0

    @pytest.mark.asyncio
    async def test_remove_rom_does_not_evict_when_not_installed(self, service, queue_cleanup):
        result = await service.remove_rom(999)
        assert result["success"] is False
        assert queue_cleanup.evicted == []

    @pytest.mark.asyncio
    async def test_uninstall_all_roms_clears_queue(self, service, uow, queue_cleanup, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(1, file_path=rom_path))

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert queue_cleanup.cleared == 1

    @pytest.mark.asyncio
    async def test_no_cleanup_dependency_is_safe(self, logger):
        """Without a ``DownloadQueueCleanup`` wired, eviction is skipped."""
        rom_files = FakeRomFileStore()
        uow = FakeUnitOfWork()
        rom_path = f"{_ROMS_BASE}/n64/g.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(7, file_path=rom_path))

        svc = RomRemovalService(
            config=RomRemovalServiceConfig(
                logger=logger,
                loop=asyncio.get_event_loop(),
                rom_file_store=rom_files,
                retrodeck_paths=FakeRetroDeckPaths(roms=_ROMS_BASE),
                download_queue_cleanup=None,
                uow_factory=FakeUnitOfWorkFactory(uow),
            ),
        )

        result = await svc.remove_rom(7)
        assert result["success"] is True

        result2 = await svc.uninstall_all_roms()
        assert result2["success"] is True


class TestBadPathRemoveRom:
    """Coverage for the ``remove_rom`` exception handler."""

    @pytest.mark.asyncio
    async def test_remove_rom_handles_filesystem_failure(self, service, uow, queue_cleanup, rom_files):
        """``remove_tree`` OSError surfaces as a failure response; the record is NOT deleted, no eviction."""
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        rom_files.remove_tree_failures.add(rom_dir)
        _seed_install(
            uow,
            _make_install(42, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"),
            platform_slug="psx",
        )

        result = await service.remove_rom(42)

        assert result["success"] is False
        assert "Failed to delete ROM files" in result["message"]
        # The install record remains because the IO helper raised before the delete UoW.
        assert uow.rom_installs.get(42) is not None
        # No queue eviction on failure.
        assert queue_cleanup.evicted == []
