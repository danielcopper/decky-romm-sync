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


def _no_corename(core_so: str) -> str | None:
    return None


def _make_service(
    tmp_path,
    *,
    sort_settings=(True, False),
    installed_roms=None,
    state_overrides=None,
    active_core=_active_core,
    get_core_name=_no_corename,
):
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
        get_active_core=active_core,
        get_core_name=get_core_name,
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


class TestResolveRetroArchCorename:
    """Unit tests for MigrationService._resolve_retroarch_corename.

    The method asks ES-DE for the active core shared object and then
    asks the RetroArch ``.info`` parser for the canonical corename. It
    must never fall back to the ES-DE display label — fail-loud is the
    contract (see Config Source Parsers wiki).
    """

    def test_happy_path_returns_retroarch_corename(self, tmp_path):
        """ES-DE returns (core_so, label); .info lookup returns the
        canonical corename; method returns (corename, core_so) — the
        corename (NOT the label) plus the underlying ``.so`` basename."""

        def active_core(system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]:
            # ES-DE label is "Snes9x - Current" — intentionally different
            # from the RetroArch corename to cover the #208 regression.
            return ("snes9x_libretro", "Snes9x - Current")

        def get_core_name(core_so: str) -> str | None:
            assert core_so == "snes9x_libretro"
            return "Snes9x"

        svc, _ = _make_service(tmp_path, active_core=active_core, get_core_name=get_core_name)
        assert svc._resolve_retroarch_corename("snes", "Zelda.sfc") == ("Snes9x", "snes9x_libretro")

    def test_active_core_returns_none_returns_none(self, tmp_path):
        """ES-DE cannot resolve the active core — method returns (None, None)."""

        def active_core(system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]:
            return (None, None)

        def get_core_name(core_so: str) -> str | None:
            # Should never be called.
            raise AssertionError("get_core_name called despite unresolved core")

        svc, _ = _make_service(tmp_path, active_core=active_core, get_core_name=get_core_name)
        assert svc._resolve_retroarch_corename("snes", "Zelda.sfc") == (None, None)

    def test_core_name_returns_none_returns_none_no_label_fallback(self, tmp_path):
        """ES-DE gives us a core_so but the .info lookup fails — method
        returns (None, core_so) so the caller can log the failed core
        (NOT the ES-DE label, which is the old bug)."""

        def active_core(system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]:
            return ("oddcore_libretro", "Some ES-DE Label")

        def get_core_name(core_so: str) -> str | None:
            return None

        svc, _ = _make_service(tmp_path, active_core=active_core, get_core_name=get_core_name)
        assert svc._resolve_retroarch_corename("odd", "Game.rom") == (None, "oddcore_libretro")

    def test_core_name_returns_empty_string_returns_none(self, tmp_path):
        """.info has ``corename = ""`` — adapter already coerces to None,
        but we also defend at the service layer with ``or None``."""

        def active_core(system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]:
            return ("blank_libretro", "Blank Label")

        def get_core_name(core_so: str) -> str | None:
            return ""

        svc, _ = _make_service(tmp_path, active_core=active_core, get_core_name=get_core_name)
        assert svc._resolve_retroarch_corename("blank", "Game.rom") == (None, "blank_libretro")

    def test_no_core_name_callback_returns_none(self, tmp_path):
        """Service constructed without ``get_core_name`` — method returns (None, None)."""

        def active_core(system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]:
            return ("snes9x_libretro", "Snes9x - Current")

        svc = MigrationService(
            state={"installed_roms": {}, "save_sort_settings": None},
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            save_state=MagicMock(),
            emit=MagicMock(),
            get_bios_files_index=lambda: {},
            get_active_core=active_core,
            # get_core_name intentionally omitted
        )
        assert svc._resolve_retroarch_corename("snes", "Zelda.sfc") == (None, None)

    def test_no_active_core_callback_returns_none(self, tmp_path):
        """Service constructed without ``get_active_core`` — method returns (None, None)."""
        svc = MigrationService(
            state={"installed_roms": {}, "save_sort_settings": None},
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            save_state=MagicMock(),
            emit=MagicMock(),
            get_bios_files_index=lambda: {},
            get_core_name=lambda core_so: "Snes9x",
        )
        assert svc._resolve_retroarch_corename("snes", "Zelda.sfc") == (None, None)


class TestSortByCoreMigrationEndToEnd:
    """End-to-end scenarios for the #208 fix.

    With sort_by_core enabled, RetroArch writes saves into a subdirectory
    named after the ``corename`` field of the core's .info file. For
    Snes9x this is ``Snes9x`` — not the ES-DE display label
    ``"Snes9x - Current"``. The migration must use the corename.
    """

    def test_uses_retroarch_corename_not_es_de_label(self, tmp_path):
        """Sort by content -> sort by core migration uses ``Snes9x``, not
        ``Snes9x - Current``, as the target subdirectory."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        # Old state: sort_by_content -> saves live at saves/snes/<ROM>.srm
        rom_file = roms_path / "snes" / "Zelda.sfc"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")
        old_save_dir = saves_path / "snes"
        old_save_dir.mkdir(parents=True)
        (old_save_dir / "Zelda.srm").write_text("save data")

        installed_roms = {
            "1": {
                "system": "snes",
                "file_path": str(rom_file),
                "platform_slug": "snes",
            }
        }
        old_settings = {"sort_by_content": True, "sort_by_core": False}
        new_settings = {"sort_by_content": False, "sort_by_core": True}

        def active_core(system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]:
            return ("snes9x_libretro", "Snes9x - Current")

        def get_core_name(core_so: str) -> str | None:
            return "Snes9x"

        svc, _ = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "installed_roms": installed_roms,
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
            active_core=active_core,
            get_core_name=get_core_name,
        )
        svc._get_saves_path = lambda: str(saves_path)
        svc._get_roms_path = lambda: str(roms_path)

        items = svc._collect_save_sorting_items(old_settings, new_settings)

        # One item produced, destination path contains "Snes9x" (not "Snes9x - Current")
        assert len(items) == 1
        _label, _old_path, new_path, _updater, _kind = items[0]
        assert os.sep + "Snes9x" + os.sep in new_path
        assert "Snes9x - Current" not in new_path

    def test_skips_rom_and_warns_when_corename_unresolved(self, tmp_path, caplog):
        """When ``.info`` lookup returns None for a ROM that needs a
        corename, the ROM is skipped and a warning is logged. The item
        is not present in the returned migration list."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        rom_file = roms_path / "odd" / "Mystery.rom"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")
        old_save_dir = saves_path / "odd"
        old_save_dir.mkdir(parents=True)
        (old_save_dir / "Mystery.srm").write_text("save")

        installed_roms = {
            "1": {
                "system": "odd",
                "file_path": str(rom_file),
                "platform_slug": "snes",  # triggers .srm extension
            }
        }
        old_settings = {"sort_by_content": True, "sort_by_core": False}
        new_settings = {"sort_by_content": False, "sort_by_core": True}

        def active_core(system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]:
            return ("oddcore_libretro", "Oddcore Label")

        def get_core_name(core_so: str) -> str | None:
            return None

        svc, _ = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "installed_roms": installed_roms,
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
            active_core=active_core,
            get_core_name=get_core_name,
        )
        svc._get_saves_path = lambda: str(saves_path)
        svc._get_roms_path = lambda: str(roms_path)

        with caplog.at_level(logging.WARNING):
            items = svc._collect_save_sorting_items(old_settings, new_settings)

        assert items == []
        assert any("unable to resolve RetroArch corename" in rec.getMessage() for rec in caplog.records), (
            "Expected a warning about unresolved corename"
        )
        assert any("core_so=oddcore_libretro" in rec.getMessage() for rec in caplog.records), (
            "Expected the warning to include core_so for diagnostics"
        )
