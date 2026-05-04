"""Tests for RomRemovalService — ROM file deletion and state cleanup."""

import asyncio
import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))
sys.path.insert(0, os.path.dirname(__file__))

# conftest.py patches decky before this import
from services.rom_removal import RomRemovalService


@pytest.fixture
def state():
    return {"installed_roms": {}}


@pytest.fixture
def save_sync_state():
    return {"saves": {}, "playtime": {}, "settings": {}}


@pytest.fixture
def logger():
    return logging.getLogger("test_rom_removal")


@pytest.fixture
def service(state, save_sync_state, logger):
    return RomRemovalService(
        state=state,
        save_sync_state=save_sync_state,
        logger=logger,
        loop=asyncio.new_event_loop(),
        save_state=MagicMock(),
        save_save_sync_state=MagicMock(),
        get_roms_path=lambda: os.path.join(os.path.expanduser("~"), "retrodeck", "roms"),
    )


@pytest.fixture(autouse=True)
async def _sync_loop(service):
    """Keep service loop in sync with the running event loop."""
    service._loop = asyncio.get_event_loop()


class TestIsSafeRomPath:
    def test_path_inside_roms_dir_is_safe(self, service, tmp_path):
        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        safe = str(tmp_path / "retrodeck" / "roms" / "n64" / "game.z64")
        assert service._is_safe_rom_path(safe) is True

    def test_path_outside_roms_dir_is_not_safe(self, service, tmp_path):
        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        outside = str(tmp_path / "evil" / "game.z64")
        assert service._is_safe_rom_path(outside) is False

    def test_roms_base_itself_is_not_safe(self, service, tmp_path):
        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        # Only 1 level deep — must be at least 2
        base = str(tmp_path / "retrodeck" / "roms" / "n64")
        assert service._is_safe_rom_path(base) is False

    def test_etc_passwd_is_not_safe(self, service, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        assert service._is_safe_rom_path("/etc/passwd") is False


class TestDeleteRomFiles:
    def test_deletes_single_file(self, service, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        rom = tmp_path / "retrodeck" / "roms" / "n64" / "game.z64"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"\x00" * 100)

        service._delete_rom_files({"file_path": str(rom)})
        assert not rom.exists()

    def test_deletes_rom_dir(self, service, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        rom_dir = tmp_path / "retrodeck" / "roms" / "psx" / "FF7"
        rom_dir.mkdir(parents=True)
        (rom_dir / "disc1.cue").write_text("cue")
        (rom_dir / "disc1.bin").write_bytes(b"\x00" * 100)

        service._delete_rom_files({"file_path": str(rom_dir / "FF7.m3u"), "rom_dir": str(rom_dir)})
        assert not rom_dir.exists()

    def test_refuses_file_outside_roms_dir(self, service, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        evil = tmp_path / "evil" / "important.txt"
        evil.parent.mkdir(parents=True)
        evil.write_text("do not delete")

        service._delete_rom_files({"file_path": str(evil)})
        assert evil.exists()

    def test_refuses_rom_dir_outside_roms_dir(self, service, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        evil_dir = tmp_path / "evil"
        evil_dir.mkdir(parents=True)
        (evil_dir / "file.txt").write_text("important")

        service._delete_rom_files({"rom_dir": str(evil_dir), "file_path": ""})
        assert evil_dir.exists()

    def test_missing_file_no_crash(self, service, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        # File doesn't exist — should not raise
        service._delete_rom_files({"file_path": str(tmp_path / "retrodeck" / "roms" / "n64" / "gone.z64")})

    def test_empty_paths_no_crash(self, service):
        # No file_path, no rom_dir
        service._delete_rom_files({"file_path": "", "rom_dir": ""})


class TestRemoveRom:
    @pytest.mark.asyncio
    async def test_removes_file_and_clears_state(self, service, state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        rom = tmp_path / "retrodeck" / "roms" / "n64" / "zelda.z64"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"\x00" * 100)

        state["installed_roms"]["42"] = {"rom_id": 42, "file_path": str(rom), "system": "n64"}

        result = await service.remove_rom(42)
        assert result["success"] is True
        assert not rom.exists()
        assert "42" not in state["installed_roms"]

    @pytest.mark.asyncio
    async def test_returns_error_if_not_installed(self, service):
        result = await service.remove_rom(999)
        assert result["success"] is False
        assert "not installed" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_accepts_string_rom_id(self, service, state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        rom = tmp_path / "retrodeck" / "roms" / "n64" / "game.z64"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"\x00" * 100)

        state["installed_roms"]["7"] = {"rom_id": 7, "file_path": str(rom), "system": "n64"}

        result = await service.remove_rom("7")
        assert result["success"] is True
        assert "7" not in state["installed_roms"]

    @pytest.mark.asyncio
    async def test_file_already_gone_cleans_state(self, service, state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": str(tmp_path / "retrodeck" / "roms" / "n64" / "gone.z64"),
            "system": "n64",
        }

        result = await service.remove_rom(42)
        assert result["success"] is True
        assert "42" not in state["installed_roms"]

    @pytest.mark.asyncio
    async def test_cleans_save_sync_state(self, service, state, save_sync_state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        rom = tmp_path / "retrodeck" / "roms" / "n64" / "zelda.z64"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"\x00" * 100)

        state["installed_roms"]["42"] = {"rom_id": 42, "file_path": str(rom), "system": "n64"}
        save_sync_state["saves"]["42"] = {"last_sync": "2024-01-01"}
        save_sync_state["playtime"]["42"] = {"total_seconds": 3600}
        # Add another ROM's state that should be preserved
        save_sync_state["saves"]["99"] = {"last_sync": "2024-02-01"}
        save_sync_state["playtime"]["99"] = {"total_seconds": 7200}

        save_calls: list[int] = []
        service._save_save_sync_state = lambda: save_calls.append(1)

        result = await service.remove_rom(42)
        assert result["success"] is True
        assert "42" not in save_sync_state["saves"]
        assert "42" not in save_sync_state["playtime"]
        assert "99" in save_sync_state["saves"]
        assert "99" in save_sync_state["playtime"]
        assert len(save_calls) == 1

    @pytest.mark.asyncio
    async def test_no_save_sync_call_if_no_matching_state(self, service, state, save_sync_state, tmp_path):
        _ = save_sync_state  # fixture ensures shared dict is initialized

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        rom = tmp_path / "retrodeck" / "roms" / "n64" / "zelda.z64"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"\x00" * 100)

        state["installed_roms"]["42"] = {"rom_id": 42, "file_path": str(rom), "system": "n64"}
        # No matching save/playtime state for ROM 42

        save_calls: list[int] = []
        service._save_save_sync_state = lambda: save_calls.append(1)

        await service.remove_rom(42)
        assert len(save_calls) == 0  # not called if nothing changed

    @pytest.mark.asyncio
    async def test_removes_rom_dir(self, service, state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        rom_dir = tmp_path / "retrodeck" / "roms" / "psx" / "FF7"
        rom_dir.mkdir(parents=True)
        (rom_dir / "FF7.m3u").write_text("disc1.cue")
        (rom_dir / "disc1.cue").write_text("cue")
        (rom_dir / "disc1.bin").write_bytes(b"\x00" * 100)

        state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": str(rom_dir / "FF7.m3u"),
            "rom_dir": str(rom_dir),
            "system": "psx",
        }

        result = await service.remove_rom(42)
        assert result["success"] is True
        assert not rom_dir.exists()
        # Parent system dir should still exist
        assert (tmp_path / "retrodeck" / "roms" / "psx").exists()

    @pytest.mark.asyncio
    async def test_path_traversal_rejected(self, service, state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        evil = tmp_path / "etc" / "passwd"
        evil.parent.mkdir(parents=True)
        evil.write_text("root:x:0:0")

        state["installed_roms"]["99"] = {"rom_id": 99, "file_path": str(evil), "system": "n64"}

        await service.remove_rom(99)
        assert evil.exists()
        assert "99" not in state["installed_roms"]

    @pytest.mark.asyncio
    async def test_removes_nested_single_file_entry(self, service, state, tmp_path):
        """Nested-single-file entries (#226) store the resolved filename in file_path with no rom_dir."""
        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        rom = tmp_path / "retrodeck" / "roms" / "dc" / "Resident Evil.chd"
        rom.parent.mkdir(parents=True)
        rom.write_bytes(b"\x00" * 100)

        state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_name": "Resident Evil.chd",
            "file_path": str(rom),
            "system": "dc",
        }

        result = await service.remove_rom(42)
        assert result["success"] is True
        assert not rom.exists()
        # Parent system dir should still exist
        assert (tmp_path / "retrodeck" / "roms" / "dc").exists()
        assert "42" not in state["installed_roms"]


class TestUninstallAllRoms:
    @pytest.mark.asyncio
    async def test_removes_all_installed(self, service, state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        file_a = roms_dir / "game_a.z64"
        file_b = roms_dir / "game_b.z64"
        file_a.write_bytes(b"\x00" * 100)
        file_b.write_bytes(b"\x00" * 100)

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(file_a), "system": "n64"},
            "2": {"rom_id": 2, "file_path": str(file_b), "system": "n64"},
        }

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 2
        assert not file_a.exists()
        assert not file_b.exists()
        assert state["installed_roms"] == {}

    @pytest.mark.asyncio
    async def test_clears_state_even_if_files_missing(self, service, state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/nonexistent.z64", "system": "n64"},
        }

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert state["installed_roms"] == {}

    @pytest.mark.asyncio
    async def test_handles_empty_state(self, service, state, tmp_path):
        _ = state  # fixture ensures shared dict is initialized

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 0

    @pytest.mark.asyncio
    async def test_cleans_save_sync_state(self, service, state, save_sync_state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        file_a = roms_dir / "game_a.z64"
        file_b = roms_dir / "game_b.z64"
        file_a.write_bytes(b"\x00" * 100)
        file_b.write_bytes(b"\x00" * 100)

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(file_a), "system": "n64"},
            "2": {"rom_id": 2, "file_path": str(file_b), "system": "n64"},
        }
        save_sync_state["saves"] = {"1": {"last_sync": "2024-01-01"}, "2": {"last_sync": "2024-02-01"}}
        save_sync_state["playtime"] = {"1": {"total_seconds": 100}, "2": {"total_seconds": 200}}

        save_calls: list[int] = []
        service._save_save_sync_state = lambda: save_calls.append(1)

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert save_sync_state["saves"] == {}
        assert save_sync_state["playtime"] == {}
        assert len(save_calls) == 1

    @pytest.mark.asyncio
    async def test_deletes_rom_directories(self, service, state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        rom_dir = tmp_path / "retrodeck" / "roms" / "psx" / "FF7"
        rom_dir.mkdir(parents=True)
        (rom_dir / "disc1.bin").write_bytes(b"\x00" * 100)

        state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": str(rom_dir / "FF7.m3u"),
                "rom_dir": str(rom_dir),
                "system": "psx",
            },
        }

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 1
        assert not rom_dir.exists()

    @pytest.mark.asyncio
    async def test_outside_roms_dir_skipped_state_still_cleared(self, service, state, save_sync_state, tmp_path):
        _ = save_sync_state  # fixture ensures shared dict is initialized

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        good_file = roms_dir / "game_a.z64"
        good_file.write_bytes(b"\x00" * 100)

        bad_file = tmp_path / "outside" / "game_b.z64"
        bad_file.parent.mkdir(parents=True)
        bad_file.write_bytes(b"\x00" * 100)

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(good_file), "system": "n64"},
            "2": {"rom_id": 2, "file_path": str(bad_file), "system": "snes"},
        }

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert not good_file.exists()
        assert bad_file.exists()  # not deleted (outside roms dir)
        # State is cleared for successfully deleted ROMs
        assert state["installed_roms"] == {}

    @pytest.mark.asyncio
    async def test_message_includes_error_count(self, service, state, tmp_path):

        service._get_roms_path = lambda: str(tmp_path / "retrodeck" / "roms")
        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        rom = roms_dir / "game.z64"
        rom.write_bytes(b"\x00" * 100)

        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(rom), "system": "n64"},
        }

        # Make deletion fail
        with patch("shutil.rmtree", side_effect=OSError("perm")), patch("os.remove", side_effect=OSError("perm")):
            result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert "errors" in result["message"]
