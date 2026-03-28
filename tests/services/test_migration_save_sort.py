"""Tests for save sort change detection and migration in MigrationService."""

from __future__ import annotations

import asyncio
import logging
import os
from unittest.mock import MagicMock

import pytest

from services.migration import MigrationService


def _active_core(system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]:
    return (None, None)


def _make_service(tmp_path, *, sort_settings=(True, False), installed_roms=None, state_overrides=None):
    """Create a MigrationService with sensible defaults for sort migration tests.

    Returns (service, save_state_mock) so callers can assert on save_state calls.
    """
    state = {
        "shortcut_registry": {},
        "installed_roms": installed_roms or {},
        "retrodeck_home_path": "",
        "save_sort_settings": None,
    }
    if state_overrides:
        state.update(state_overrides)

    saves_path = str(tmp_path / "saves")
    roms_path = str(tmp_path / "roms")

    save_state_mock = MagicMock()

    svc = MigrationService(
        state=state,
        loop=asyncio.get_event_loop(),
        logger=logging.getLogger("test"),
        save_state=save_state_mock,
        emit=MagicMock(),
        get_bios_files_index=lambda: {},
        get_retrodeck_home=lambda: str(tmp_path),
        get_saves_path=lambda: saves_path,
        get_bios_path=lambda: str(tmp_path / "bios"),
        get_retroarch_save_sorting=lambda: sort_settings,
        get_roms_path=lambda: roms_path,
        get_active_core=_active_core,
    )
    return svc, save_state_mock


class TestDetectSaveSortChange:
    def test_first_run_stores_settings(self, tmp_path):
        """First run (stored=None) stores current settings, no event emitted."""
        svc, save_state_mock = _make_service(tmp_path, sort_settings=(True, False))
        mock_loop = MagicMock()
        svc._loop = mock_loop

        svc.detect_save_sort_change()

        assert svc._state["save_sort_settings"] == {"sort_by_content": True, "sort_by_core": False}
        assert "save_sort_settings_previous" not in svc._state
        mock_loop.create_task.assert_not_called()
        save_state_mock.assert_called_once()

    def test_no_change_no_event(self, tmp_path):
        """Stored settings equal current — no event, no state mutation."""
        svc, save_state_mock = _make_service(
            tmp_path,
            sort_settings=(True, False),
            state_overrides={"save_sort_settings": {"sort_by_content": True, "sort_by_core": False}},
        )
        mock_loop = MagicMock()
        svc._loop = mock_loop

        svc.detect_save_sort_change()

        mock_loop.create_task.assert_not_called()
        save_state_mock.assert_not_called()
        assert "save_sort_settings_previous" not in svc._state

    def test_change_emits_event(self, tmp_path):
        """Settings changed — emits event, stores old + new."""
        old = {"sort_by_content": True, "sort_by_core": False}
        svc, save_state_mock = _make_service(
            tmp_path,
            sort_settings=(False, True),
            state_overrides={"save_sort_settings": old},
        )

        _tasks = []

        def _close_task(coro):
            coro.close()
            _tasks.append(coro)
            return MagicMock()

        mock_loop = MagicMock()
        mock_loop.create_task = _close_task
        svc._loop = mock_loop

        svc.detect_save_sort_change()

        assert svc._state["save_sort_settings"] == {"sort_by_content": False, "sort_by_core": True}
        assert svc._state["save_sort_settings_previous"] == old
        assert len(_tasks) == 1
        save_state_mock.assert_called_once()

    def test_no_callback_noop(self, tmp_path):
        """No get_retroarch_save_sorting callback — method is a no-op."""
        save_state_mock = MagicMock()
        svc = MigrationService(
            state={"save_sort_settings": None, "installed_roms": {}},
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            save_state=save_state_mock,
            emit=MagicMock(),
            get_bios_files_index=lambda: {},
        )
        # Should not raise, no state changes
        svc.detect_save_sort_change()
        save_state_mock.assert_not_called()


class TestCollectSaveSortingItems:
    def test_finds_existing_saves(self, tmp_path):
        """ROM installed with save file at old sort path — item returned."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        rom_file = roms_path / "gba" / "Pokemon.gba"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")

        # sort_by_content=True puts saves in saves/gba/Pokemon.srm
        old_save_dir = saves_path / "gba"
        old_save_dir.mkdir(parents=True)
        (old_save_dir / "Pokemon.srm").write_text("save")

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        svc, _ = _make_service(
            tmp_path,
            sort_settings=(False, False),
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings": {"sort_by_content": False, "sort_by_core": False},
                "installed_roms": installed_roms,
            },
        )
        svc._get_saves_path = lambda: str(saves_path)
        svc._get_roms_path = lambda: str(roms_path)

        old_settings = {"sort_by_content": True, "sort_by_core": False}
        new_settings = {"sort_by_content": False, "sort_by_core": False}
        items = svc._collect_save_sorting_items(old_settings, new_settings)

        assert len(items) == 1
        label, old_path, _new_path, _, kind = items[0]
        assert label == "Pokemon.srm"
        assert kind == "save"
        assert os.path.basename(old_path) == "Pokemon.srm"

    def test_skips_same_dir(self, tmp_path):
        """Old and new dirs are the same — no items returned."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        rom_file = roms_path / "gba" / "Pokemon.gba"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        svc, _ = _make_service(tmp_path, installed_roms=installed_roms)
        svc._get_saves_path = lambda: str(saves_path)
        svc._get_roms_path = lambda: str(roms_path)

        # Same settings -> same dir
        same_settings = {"sort_by_content": True, "sort_by_core": False}
        items = svc._collect_save_sorting_items(same_settings, same_settings)

        assert items == []

    def test_skips_missing_files(self, tmp_path):
        """ROM installed but no save file exists — items is empty."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        rom_file = roms_path / "gba" / "Pokemon.gba"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        svc, _ = _make_service(tmp_path, installed_roms=installed_roms)
        svc._get_saves_path = lambda: str(saves_path)
        svc._get_roms_path = lambda: str(roms_path)

        old_settings = {"sort_by_content": True, "sort_by_core": False}
        new_settings = {"sort_by_content": False, "sort_by_core": False}
        items = svc._collect_save_sorting_items(old_settings, new_settings)

        assert items == []


class TestSaveSortMigrationStatus:
    @pytest.mark.asyncio
    async def test_not_pending_when_no_previous(self, tmp_path):
        """No save_sort_settings_previous in state — returns {pending: False}."""
        svc, _ = _make_service(tmp_path)

        result = await svc.get_save_sort_migration_status()

        assert result == {"pending": False}

    @pytest.mark.asyncio
    async def test_pending_with_count(self, tmp_path):
        """Has previous settings and a save file — returns pending with saves_count."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        rom_file = roms_path / "gba" / "Pokemon.gba"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")

        # Save exists at old location (sort_by_content=True -> saves/gba/)
        old_save_dir = saves_path / "gba"
        old_save_dir.mkdir(parents=True)
        (old_save_dir / "Pokemon.srm").write_text("save")

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        old_settings = {"sort_by_content": True, "sort_by_core": False}
        new_settings = {"sort_by_content": False, "sort_by_core": False}
        svc, _ = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "installed_roms": installed_roms,
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
        )
        svc._get_saves_path = lambda: str(saves_path)
        svc._get_roms_path = lambda: str(roms_path)

        result = await svc.get_save_sort_migration_status()

        assert result["pending"] is True
        assert result["saves_count"] == 1
        assert result["old_settings"] == old_settings
        assert result["new_settings"] == new_settings


class TestMigrateSaveSortFiles:
    @pytest.mark.asyncio
    async def test_happy_path_moves_file(self, tmp_path):
        """Save file at old sort path is moved to new sort path, previous state cleared."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        rom_file = roms_path / "gba" / "Pokemon.gba"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")

        # Save exists at old location (sort_by_content=True -> saves/gba/Pokemon.srm)
        old_save_dir = saves_path / "gba"
        old_save_dir.mkdir(parents=True)
        old_save = old_save_dir / "Pokemon.srm"
        old_save.write_text("save data")

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        old_settings = {"sort_by_content": True, "sort_by_core": False}
        new_settings = {"sort_by_content": False, "sort_by_core": False}
        svc, _ = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "installed_roms": installed_roms,
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
        )
        svc._get_saves_path = lambda: str(saves_path)
        svc._get_roms_path = lambda: str(roms_path)

        result = await svc.migrate_save_sort_files()

        assert result["success"] is True
        assert result["saves_moved"] == 1
        # File moved to new location (sort_by_content=False -> saves/Pokemon.srm)
        new_save = saves_path / "Pokemon.srm"
        assert new_save.exists()
        assert not old_save.exists()
        assert "save_sort_settings_previous" not in svc._state

    @pytest.mark.asyncio
    async def test_conflicts_return_confirmation(self, tmp_path):
        """Save at both old and new location — returns needs_confirmation without moving."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        rom_file = roms_path / "gba" / "Pokemon.gba"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")

        # Save exists at old location
        old_save_dir = saves_path / "gba"
        old_save_dir.mkdir(parents=True)
        old_save = old_save_dir / "Pokemon.srm"
        old_save.write_text("old save")

        # Save also exists at new location
        new_save = saves_path / "Pokemon.srm"
        new_save.write_text("new save")

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        old_settings = {"sort_by_content": True, "sort_by_core": False}
        new_settings = {"sort_by_content": False, "sort_by_core": False}
        svc, _ = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "installed_roms": installed_roms,
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
        )
        svc._get_saves_path = lambda: str(saves_path)
        svc._get_roms_path = lambda: str(roms_path)

        result = await svc.migrate_save_sort_files()

        assert result["success"] is False
        assert result["needs_confirmation"] is True
        assert result["conflict_count"] == 1
        # conflicts is now a list of dicts with file details
        conflicts = result["conflicts"]
        assert len(conflicts) == 1
        detail = conflicts[0]
        assert detail["filename"] == "Pokemon.srm"
        assert detail["old_size"] == len(b"old save")
        assert detail["new_size"] == len(b"new save")
        assert "old_mtime" in detail
        assert "new_mtime" in detail
        assert "old_path" in detail
        assert "new_path" in detail
        # Files untouched
        assert old_save.exists()
        assert new_save.read_text() == "new save"

    @pytest.mark.asyncio
    async def test_clears_previous_on_success(self, tmp_path):
        """After successful migration save_sort_settings_previous is removed from state."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        old_settings = {"sort_by_content": True, "sort_by_core": False}
        new_settings = {"sort_by_content": False, "sort_by_core": False}
        # No installed ROMs — migration runs with 0 items but still succeeds
        svc, _ = _make_service(
            tmp_path,
            state_overrides={
                "installed_roms": {},
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
        )
        svc._get_saves_path = lambda: str(saves_path)
        svc._get_roms_path = lambda: str(roms_path)

        result = await svc.migrate_save_sort_files()

        assert result["success"] is True
        assert "save_sort_settings_previous" not in svc._state

    @pytest.mark.asyncio
    async def test_no_migration_needed(self, tmp_path):
        """No previous settings — returns not needed."""
        svc, _ = _make_service(tmp_path)

        result = await svc.migrate_save_sort_files()

        assert result["success"] is False
        assert "No save sorting migration needed" in result["message"]
