"""Tests for RomRemovalService — ROM file deletion and state cleanup."""

import asyncio
import logging
import os
import sys
from unittest.mock import MagicMock

import pytest
from conftest import FakeDownloadQueueCleanup, FakeRetroDeckPaths, FakeRomFileStore

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))
sys.path.insert(0, os.path.dirname(__file__))

# conftest.py patches decky before this import
from domain.save_state import SaveSyncState
from services.rom_removal import RomRemovalService, RomRemovalServiceConfig

# Synthetic roms-base path used by the fake fs throughout this module.
_ROMS_BASE = "/retrodeck/roms"


@pytest.fixture
def state():
    return {"installed_roms": {}}


@pytest.fixture
def save_sync_state():
    return SaveSyncState()


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
def service(state, save_sync_state, logger, queue_cleanup, rom_files):
    return RomRemovalService(
        config=RomRemovalServiceConfig(
            state=state,
            save_sync_state=save_sync_state,
            logger=logger,
            loop=asyncio.new_event_loop(),
            state_persister=MagicMock(),
            save_sync_state_writer=MagicMock(),
            rom_file_store=rom_files,
            retrodeck_paths=FakeRetroDeckPaths(roms=_ROMS_BASE),
            download_queue_cleanup=queue_cleanup,
        ),
    )


@pytest.fixture(autouse=True)
async def _sync_loop(service):
    """Keep service loop in sync with the running event loop."""
    service._loop = asyncio.get_event_loop()


class TestDeleteRomFiles:
    def test_deletes_single_file(self, service, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"\x00" * 100

        service._delete_rom_files({"file_path": rom_path})

        assert rom_path not in rom_files.files
        assert rom_files.remove_file_calls == [rom_path]

    def test_deletes_rom_dir(self, service, rom_files):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.cue"] = b"cue"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100

        service._delete_rom_files({"file_path": f"{rom_dir}/FF7.m3u", "rom_dir": rom_dir})

        assert f"{rom_dir}/disc1.cue" not in rom_files.files
        assert f"{rom_dir}/disc1.bin" not in rom_files.files
        assert rom_files.remove_tree_calls == [rom_dir]

    def test_refuses_file_outside_roms_dir(self, service, rom_files):
        evil = "/evil/important.txt"
        rom_files.files[evil] = b"do not delete"

        service._delete_rom_files({"file_path": evil})

        assert evil in rom_files.files
        assert rom_files.remove_file_calls == []
        assert rom_files.remove_tree_calls == []

    def test_refuses_rom_dir_outside_roms_dir(self, service, rom_files):
        evil_dir = "/evil"
        rom_files.files[f"{evil_dir}/file.txt"] = b"important"

        service._delete_rom_files({"rom_dir": evil_dir, "file_path": ""})

        assert f"{evil_dir}/file.txt" in rom_files.files
        assert rom_files.remove_tree_calls == []

    def test_missing_file_no_crash(self, service):
        # File doesn't exist — should not raise and should not call any I/O
        service._delete_rom_files({"file_path": f"{_ROMS_BASE}/n64/gone.z64"})

    def test_empty_paths_no_crash(self, service):
        # No file_path, no rom_dir
        service._delete_rom_files({"file_path": "", "rom_dir": ""})


class TestRemoveRom:
    @pytest.mark.asyncio
    async def test_removes_file_and_clears_state(self, service, state, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        state["installed_roms"]["42"] = {"rom_id": 42, "file_path": rom_path, "system": "n64"}

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert rom_path not in rom_files.files
        assert "42" not in state["installed_roms"]

    @pytest.mark.asyncio
    async def test_returns_error_if_not_installed(self, service):
        result = await service.remove_rom(999)
        assert result["success"] is False
        assert "not installed" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_accepts_string_rom_id(self, service, state, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        state["installed_roms"]["7"] = {"rom_id": 7, "file_path": rom_path, "system": "n64"}

        result = await service.remove_rom("7")

        assert result["success"] is True
        assert "7" not in state["installed_roms"]

    @pytest.mark.asyncio
    async def test_file_already_gone_cleans_state(self, service, state):
        state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": f"{_ROMS_BASE}/n64/gone.z64",
            "system": "n64",
        }

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert "42" not in state["installed_roms"]

    @pytest.mark.asyncio
    async def test_cleans_save_sync_state(self, service, state, save_sync_state, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        state["installed_roms"]["42"] = {"rom_id": 42, "file_path": rom_path, "system": "n64"}
        save_sync_state.saves["42"] = {"last_sync": "2024-01-01"}
        save_sync_state.playtime["42"] = {"total_seconds": 3600}
        # Add another ROM's state that should be preserved
        save_sync_state.saves["99"] = {"last_sync": "2024-02-01"}
        save_sync_state.playtime["99"] = {"total_seconds": 7200}

        save_calls: list[int] = []

        class _Recorder:
            def save_state(self) -> None:
                save_calls.append(1)

        service._save_sync_state_writer = _Recorder()

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert "42" not in save_sync_state.saves
        assert "42" not in save_sync_state.playtime
        assert "99" in save_sync_state.saves
        assert "99" in save_sync_state.playtime
        assert len(save_calls) == 1

    @pytest.mark.asyncio
    async def test_no_save_sync_call_if_no_matching_state(self, service, state, save_sync_state, rom_files):
        _ = save_sync_state  # fixture ensures shared dict is initialized
        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        state["installed_roms"]["42"] = {"rom_id": 42, "file_path": rom_path, "system": "n64"}
        # No matching save/playtime state for ROM 42

        save_calls: list[int] = []

        class _Recorder:
            def save_state(self) -> None:
                save_calls.append(1)

        service._save_sync_state_writer = _Recorder()

        await service.remove_rom(42)
        assert len(save_calls) == 0  # not called if nothing changed

    @pytest.mark.asyncio
    async def test_removes_rom_dir(self, service, state, rom_files):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/FF7.m3u"] = b"disc1.cue"
        rom_files.files[f"{rom_dir}/disc1.cue"] = b"cue"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        # Mark the parent system dir as existing so we can assert it's
        # preserved.
        rom_files.dirs.add(f"{_ROMS_BASE}/psx")

        state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": f"{rom_dir}/FF7.m3u",
            "rom_dir": rom_dir,
            "system": "psx",
        }

        result = await service.remove_rom(42)

        assert result["success"] is True
        # rom_dir gone
        assert all(not p.startswith(rom_dir + "/") for p in rom_files.files)
        # Parent system dir still tracked
        assert f"{_ROMS_BASE}/psx" in rom_files.dirs

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, service, state, rom_files):
        evil = "/etc/passwd"
        rom_files.files[evil] = b"root:x:0:0"
        state["installed_roms"]["99"] = {"rom_id": 99, "file_path": evil, "system": "n64"}

        await service.remove_rom(99)

        assert evil in rom_files.files
        assert "99" not in state["installed_roms"]

    @pytest.mark.asyncio
    async def test_removes_nested_single_file_entry(self, service, state, rom_files):
        """Nested-single-file entries (#226) store the resolved filename in file_path with no rom_dir."""
        rom_path = f"{_ROMS_BASE}/dc/Resident Evil.chd"
        rom_files.files[rom_path] = b"\x00" * 100
        rom_files.dirs.add(f"{_ROMS_BASE}/dc")

        state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_name": "Resident Evil.chd",
            "file_path": rom_path,
            "system": "dc",
        }

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert rom_path not in rom_files.files
        # Parent system dir still tracked
        assert f"{_ROMS_BASE}/dc" in rom_files.dirs
        assert "42" not in state["installed_roms"]


class TestUninstallAllRoms:
    @pytest.mark.asyncio
    async def test_removes_all_installed(self, service, state, rom_files):
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        rom_files.files[file_a] = b"\x00" * 100
        rom_files.files[file_b] = b"\x00" * 100

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": file_a, "system": "n64"},
            "2": {"rom_id": 2, "file_path": file_b, "system": "n64"},
        }

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["removed_count"] == 2
        assert file_a not in rom_files.files
        assert file_b not in rom_files.files
        assert state["installed_roms"] == {}

    @pytest.mark.asyncio
    async def test_clears_state_even_if_files_missing(self, service, state):
        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/nonexistent.z64", "system": "n64"},
        }

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert state["installed_roms"] == {}

    @pytest.mark.asyncio
    async def test_handles_empty_state(self, service, state):
        _ = state  # fixture ensures shared dict is initialized
        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 0

    @pytest.mark.asyncio
    async def test_cleans_save_sync_state(self, service, state, save_sync_state, rom_files):
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        rom_files.files[file_a] = b"\x00" * 100
        rom_files.files[file_b] = b"\x00" * 100

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": file_a, "system": "n64"},
            "2": {"rom_id": 2, "file_path": file_b, "system": "n64"},
        }
        save_sync_state.saves = {"1": {"last_sync": "2024-01-01"}, "2": {"last_sync": "2024-02-01"}}
        save_sync_state.playtime = {"1": {"total_seconds": 100}, "2": {"total_seconds": 200}}

        save_calls: list[int] = []

        class _Recorder:
            def save_state(self) -> None:
                save_calls.append(1)

        service._save_sync_state_writer = _Recorder()

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert save_sync_state.saves == {}
        assert save_sync_state.playtime == {}
        assert len(save_calls) == 1

    @pytest.mark.asyncio
    async def test_deletes_rom_directories(self, service, state, rom_files):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100

        state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": f"{rom_dir}/FF7.m3u",
                "rom_dir": rom_dir,
                "system": "psx",
            },
        }

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 1
        assert all(not p.startswith(rom_dir + "/") for p in rom_files.files)

    @pytest.mark.asyncio
    async def test_outside_roms_dir_skipped_state_still_cleared(self, service, state, save_sync_state, rom_files):
        _ = save_sync_state  # fixture ensures shared dict is initialized
        good_file = f"{_ROMS_BASE}/n64/game_a.z64"
        rom_files.files[good_file] = b"\x00" * 100

        bad_file = "/outside/game_b.z64"
        rom_files.files[bad_file] = b"\x00" * 100

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": good_file, "system": "n64"},
            "2": {"rom_id": 2, "file_path": bad_file, "system": "snes"},
        }

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert good_file not in rom_files.files
        assert bad_file in rom_files.files  # not deleted (outside roms dir)
        # State is cleared for successfully deleted ROMs
        assert state["installed_roms"] == {}

    @pytest.mark.asyncio
    async def test_partial_failure_reports_errors_and_not_success(self, service, state, rom_files):
        """Bad path: one of three deletions fails → ``success`` is False and ``errors`` lists the failure."""
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        file_c = f"{_ROMS_BASE}/n64/game_c.z64"
        for p in (file_a, file_b, file_c):
            rom_files.files[p] = b"\x00" * 100
        rom_files.remove_file_failures.add(file_b)

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": file_a, "system": "n64"},
            "2": {"rom_id": 2, "file_path": file_b, "system": "n64"},
            "3": {"rom_id": 3, "file_path": file_c, "system": "n64"},
        }

        result = await service.uninstall_all_roms()

        assert result["success"] is False
        assert result["removed_count"] == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["rom_id"] == "2"
        assert "game_b.z64" in result["errors"][0]["error"]
        # State for successful deletions is cleared; the failing entry is left
        # in place so the user can retry.
        assert "1" not in state["installed_roms"]
        assert "2" in state["installed_roms"]
        assert "3" not in state["installed_roms"]

    @pytest.mark.asyncio
    async def test_all_success_returns_empty_errors(self, service, state, rom_files):
        """Happy path: all 3 deletions succeed → ``success`` is True and ``errors`` is empty."""
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        file_c = f"{_ROMS_BASE}/n64/game_c.z64"
        for p in (file_a, file_b, file_c):
            rom_files.files[p] = b"\x00" * 100

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": file_a, "system": "n64"},
            "2": {"rom_id": 2, "file_path": file_b, "system": "n64"},
            "3": {"rom_id": 3, "file_path": file_c, "system": "n64"},
        }

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["removed_count"] == 3
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_empty_state_returns_success_with_empty_errors(self, service, state):
        """Edge: no installed ROMs → ``success`` is True and ``errors`` is empty."""
        _ = state  # fixture ensures shared dict is initialized
        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["removed_count"] == 0
        assert result["errors"] == []


class TestDownloadQueueCleanup:
    """Eviction of the download queue on successful ROM removal."""

    @pytest.mark.asyncio
    async def test_remove_rom_evicts_queue_on_success(self, service, state, queue_cleanup, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        state["installed_roms"]["42"] = {"rom_id": 42, "file_path": rom_path, "system": "n64"}

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
    async def test_uninstall_all_roms_clears_queue(self, service, state, queue_cleanup, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"\x00" * 100

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": rom_path, "system": "n64"},
        }

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert queue_cleanup.cleared == 1

    @pytest.mark.asyncio
    async def test_no_cleanup_dependency_is_safe(self, state, save_sync_state, logger):
        """Without a ``DownloadQueueCleanup`` wired, eviction is skipped."""
        rom_files = FakeRomFileStore()
        rom_path = f"{_ROMS_BASE}/n64/g.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        state["installed_roms"]["7"] = {"rom_id": 7, "file_path": rom_path, "system": "n64"}

        svc = RomRemovalService(
            config=RomRemovalServiceConfig(
                state=state,
                save_sync_state=save_sync_state,
                logger=logger,
                loop=asyncio.get_event_loop(),
                state_persister=MagicMock(),
                save_sync_state_writer=MagicMock(),
                rom_file_store=rom_files,
                retrodeck_paths=FakeRetroDeckPaths(roms=_ROMS_BASE),
                download_queue_cleanup=None,
            ),
        )

        result = await svc.remove_rom(7)
        assert result["success"] is True

        result2 = await svc.uninstall_all_roms()
        assert result2["success"] is True


class TestBadPathRemoveRom:
    """Coverage for the previously-untested ``remove_rom`` exception handler."""

    @pytest.mark.asyncio
    async def test_remove_rom_handles_filesystem_failure(self, service, state, queue_cleanup, rom_files):
        """``remove_tree`` OSError surfaces as a failure response with no eviction."""
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        rom_files.remove_tree_failures.add(rom_dir)

        state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": f"{rom_dir}/FF7.m3u",
            "rom_dir": rom_dir,
            "system": "psx",
        }

        result = await service.remove_rom(42)

        assert result["success"] is False
        assert "Failed to delete ROM files" in result["message"]
        # State entry remains because the IO helper raised before state mutation.
        assert "42" in state["installed_roms"]
        # No queue eviction on failure.
        assert queue_cleanup.evicted == []
