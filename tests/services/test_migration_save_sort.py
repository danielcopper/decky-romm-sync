"""Tests for save sort change detection and migration in MigrationService."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_migration_file_store import FakeMigrationFileStore
from fakes.fake_relaunch_options_resolver import FakeRelaunchOptionsResolver
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from adapters.migration_file import MigrationFileAdapter
from domain.rom import Rom
from domain.rom_install import RomInstall
from domain.save_layout import ContentDir, InSaveDir, SaveLayout
from services.migration import MigrationService, MigrationServiceConfig

if TYPE_CHECKING:
    from models.state import SaveSortSettings


def _no_corename(core_so: str) -> str | None:
    return None


def _seed_installs(uow, installed_roms: dict[str, Any]) -> None:
    """Seed Rom (FK parent) + RomInstall rows from the legacy installed_roms dict shape."""
    with uow:
        for rom_id_str, entry in installed_roms.items():
            rom_id = int(rom_id_str)
            file_path = entry.get("file_path", "")
            uow.roms.save(
                Rom(
                    rom_id=rom_id,
                    platform_slug=entry.get("platform_slug", "") or entry.get("system", "x"),
                    name=f"Game {rom_id}",
                    fs_name=f"game{rom_id}",
                    shortcut_app_id=None,
                    last_synced_at="2025-01-01T00:00:00",
                )
            )
            uow.rom_installs.save(
                RomInstall.mark_installed(
                    rom_id=rom_id,
                    file_path=file_path,
                    rom_dir=entry.get("rom_dir"),
                    platform_slug=entry.get("platform_slug", ""),
                    system=entry.get("system", ""),
                    installed_at="2025-01-01T00:00:00",
                )
            )


def _read_marker(uow, key: str) -> dict[str, Any]:
    """Decode a save-sort kv_config marker, asserting it is present."""
    raw = uow.kv_config.get(key)
    assert raw is not None, f"expected kv_config marker {key!r} to be set"
    return json.loads(raw)


def _seed_markers(uow, state_overrides: dict[str, Any]) -> None:
    """Write the save-sort kv_config markers from the legacy state_overrides shape."""
    with uow:
        if "save_sort_settings" in state_overrides:
            uow.kv_config.set("save_sort_settings", json.dumps(state_overrides["save_sort_settings"]))
        if "save_sort_settings_previous" in state_overrides:
            uow.kv_config.set("save_sort_settings_previous", json.dumps(state_overrides["save_sort_settings_previous"]))


def _make_service(
    tmp_path,
    *,
    sort_settings=(True, False),
    save_layout=None,
    installed_roms=None,
    state_overrides=None,
    active_core=None,
    get_core_name=_no_corename,
    migration_file_store=None,
):
    """Create a MigrationService with sensible defaults for sort migration tests.

    Returns (service, uow) so callers can seed installs/markers and assert on the
    commit flag and the kv_config values. Pass ``migration_file_store`` to swap
    the real ``MigrationFileAdapter`` for a fake when a test needs failure injection.

    ``sort_settings`` is the ``(sort_by_content, sort_by_core)`` tuple the
    ``InSaveDir`` layout is built from. Pass an explicit ``save_layout``
    (e.g. ``ContentDir()``) to override the supported-layout default.
    """
    uow = FakeUnitOfWork()
    if installed_roms:
        _seed_installs(uow, installed_roms)
    if state_overrides:
        _seed_markers(uow, state_overrides)

    saves_path = str(tmp_path / "saves")
    roms_path = str(tmp_path / "roms")

    layout: SaveLayout = (
        save_layout
        if save_layout is not None
        else InSaveDir(sort_by_content=sort_settings[0], sort_by_core=sort_settings[1])
    )

    svc = MigrationService(
        config=MigrationServiceConfig(
            migration_file_store=migration_file_store if migration_file_store is not None else MigrationFileAdapter(),
            settings={},
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            settings_persister=MagicMock(),
            emit=MagicMock(),
            get_bios_files_index=dict,
            retrodeck_paths=FakeRetroDeckPaths(
                saves=saves_path,
                roms=roms_path,
                bios=str(tmp_path / "bios"),
                home=str(tmp_path),
            ),
            get_save_layout=lambda: layout,
            active_core=active_core if active_core is not None else FakeActiveCoreResolver(default=(None, None)),
            relaunch_options=FakeRelaunchOptionsResolver(),
            get_core_name=get_core_name,
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
        ),
    )
    return svc, uow


class TestDetectSaveSortChange:
    def test_first_run_stores_settings(self, tmp_path):
        """First run (stored=None) stores current settings, no event emitted."""
        svc, uow = _make_service(tmp_path, sort_settings=(True, False))
        mock_loop = MagicMock()
        svc._loop = mock_loop

        layout = svc.detect_save_sort_change()

        # Returns the live InSaveDir layout it observed.
        assert layout == InSaveDir(sort_by_content=True, sort_by_core=False)
        with uow:
            assert _read_marker(uow, "save_sort_settings") == {
                "sort_by_content": True,
                "sort_by_core": False,
            }
            assert uow.kv_config.get("save_sort_settings_previous") is None
        assert uow.committed is True
        mock_loop.create_task.assert_not_called()

    def test_no_change_no_event(self, tmp_path):
        """Stored settings equal current — no event, no marker write."""
        svc, uow = _make_service(
            tmp_path,
            sort_settings=(True, False),
            state_overrides={"save_sort_settings": {"sort_by_content": True, "sort_by_core": False}},
        )
        mock_loop = MagicMock()
        svc._loop = mock_loop
        set_count_before = uow.kv_config.set_count

        layout = svc.detect_save_sort_change()

        assert layout == InSaveDir(sort_by_content=True, sort_by_core=False)
        mock_loop.create_task.assert_not_called()
        # No marker write occurred (no new kv_config.set beyond the seed).
        assert uow.kv_config.set_count == set_count_before
        with uow:
            assert uow.kv_config.get("save_sort_settings_previous") is None


class TestDetectSaveSortChangeContentDir:
    """ContentDir layout (savefiles_in_content_dir=true) is unsupported — the
    detect pass must never touch the kv_config sort markers, must return the
    ContentDir layout for the SyncEngine gate, and must warn only once."""

    def test_content_dir_returns_layout_and_writes_no_markers(self, tmp_path):
        """ContentDir → no kv_config write at all, returns ContentDir()."""
        svc, uow = _make_service(tmp_path, save_layout=ContentDir())
        mock_loop = MagicMock()
        svc._loop = mock_loop
        set_count_before = uow.kv_config.set_count

        layout = svc.detect_save_sort_change()

        assert isinstance(layout, ContentDir)
        # No sort markers written — content-dir saves are outside the saves tree.
        assert uow.kv_config.set_count == set_count_before
        with uow:
            assert uow.kv_config.get("save_sort_settings") is None
            assert uow.kv_config.get("save_sort_settings_previous") is None
        mock_loop.create_task.assert_not_called()

    def test_content_dir_does_not_overwrite_existing_sort_markers(self, tmp_path):
        """A pre-existing InSaveDir observation survives a ContentDir detect."""
        svc, uow = _make_service(
            tmp_path,
            save_layout=ContentDir(),
            state_overrides={"save_sort_settings": {"sort_by_content": True, "sort_by_core": False}},
        )
        set_count_before = uow.kv_config.set_count

        layout = svc.detect_save_sort_change()

        assert isinstance(layout, ContentDir)
        assert uow.kv_config.set_count == set_count_before
        with uow:
            assert _read_marker(uow, "save_sort_settings") == {"sort_by_content": True, "sort_by_core": False}

    def test_content_dir_warns_once_per_process(self, tmp_path, caplog):
        """The unsupported-state warning fires once, not on every detect pass."""
        svc, _ = _make_service(tmp_path, save_layout=ContentDir())

        with caplog.at_level(logging.WARNING):
            svc.detect_save_sort_change()
            svc.detect_save_sort_change()
            svc.detect_save_sort_change()

        content_dir_warnings = [r for r in caplog.records if "savefiles_in_content_dir" in r.getMessage()]
        assert len(content_dir_warnings) == 1

    def test_change_emits_event(self, tmp_path):
        """Settings changed — emits event, stores old + new."""
        old = {"sort_by_content": True, "sort_by_core": False}
        # AsyncMock returns a coroutine when called — required because
        # detect_save_sort_change schedules the emit coroutine via
        # asyncio.run_coroutine_threadsafe, which validates that its
        # first arg is an actual coroutine (#238 review finding 1).
        svc, uow = _make_service(
            tmp_path,
            sort_settings=(False, True),
            state_overrides={"save_sort_settings": old},
        )
        svc._emit = AsyncMock()

        # Stub run_coroutine_threadsafe at the module level so we can
        # observe scheduling without needing a running event loop. The
        # stub closes the coroutine to avoid "never awaited" warnings.
        scheduled: list[Any] = []

        def fake_schedule(coro, loop):
            coro.close()
            scheduled.append(coro)
            return MagicMock()

        import services.migration as migration_module

        original = migration_module.asyncio.run_coroutine_threadsafe
        migration_module.asyncio.run_coroutine_threadsafe = fake_schedule  # type: ignore[assignment]
        try:
            layout = svc.detect_save_sort_change()
        finally:
            migration_module.asyncio.run_coroutine_threadsafe = original  # type: ignore[assignment]

        assert layout == InSaveDir(sort_by_content=False, sort_by_core=True)
        with uow:
            assert _read_marker(uow, "save_sort_settings") == {
                "sort_by_content": False,
                "sort_by_core": True,
            }
            assert _read_marker(uow, "save_sort_settings_previous") == old
        assert uow.committed is True
        assert len(scheduled) == 1


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
        svc, uow = _make_service(
            tmp_path,
            sort_settings=(False, False),
            installed_roms=installed_roms,
            state_overrides={"save_sort_settings": {"sort_by_content": False, "sort_by_core": False}},
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        with uow:
            installs = list(uow.rom_installs.iter_all())
        items = svc._collect_save_sorting_items(old_settings, new_settings, installs)

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
        svc, uow = _make_service(tmp_path, installed_roms=installed_roms)
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        # Same settings -> same dir
        same_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        with uow:
            installs = list(uow.rom_installs.iter_all())
        items = svc._collect_save_sorting_items(same_settings, same_settings, installs)

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
        svc, uow = _make_service(tmp_path, installed_roms=installed_roms)
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        with uow:
            installs = list(uow.rom_installs.iter_all())
        items = svc._collect_save_sorting_items(old_settings, new_settings, installs)

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
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        svc, _ = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

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
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        svc, uow = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        result = await svc.migrate_save_sort_files()

        assert result["success"] is True
        assert result["saves_moved"] == 1
        # File moved to new location (sort_by_content=False -> saves/Pokemon.srm)
        new_save = saves_path / "Pokemon.srm"
        assert new_save.exists()
        assert not old_save.exists()
        with uow:
            assert uow.kv_config.get("save_sort_settings_previous") is None

    @pytest.mark.asyncio
    async def test_conflict_destination_newer_deletes_old(self, tmp_path):
        """Mid-game setting change edge case: newer file at destination wins,
        stale orphan at old location is removed."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        rom_file = roms_path / "gba" / "Pokemon.gba"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")

        # Old (stale) save at old location
        old_save_dir = saves_path / "gba"
        old_save_dir.mkdir(parents=True)
        old_save = old_save_dir / "Pokemon.srm"
        old_save.write_text("stale pre-change")

        # New (fresh) save at new location
        new_save = saves_path / "Pokemon.srm"
        new_save.write_text("fresh in-game save")

        # Make new_save newer than old_save
        os.utime(str(old_save), (1_000_000, 1_000_000))
        os.utime(str(new_save), (2_000_000, 2_000_000))

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        svc, uow = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        result = await svc.migrate_save_sort_files()

        assert result["success"] is True
        assert result["saves_moved"] == 1
        # Destination (newer) preserved unchanged, old orphan deleted.
        assert new_save.read_text() == "fresh in-game save"
        assert not old_save.exists()
        with uow:
            assert uow.kv_config.get("save_sort_settings_previous") is None

    @pytest.mark.asyncio
    async def test_conflict_source_newer_overwrites(self, tmp_path):
        """Rare case: source is newer than destination — atomically overwrite."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        rom_file = roms_path / "gba" / "Pokemon.gba"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("rom")

        old_save_dir = saves_path / "gba"
        old_save_dir.mkdir(parents=True)
        old_save = old_save_dir / "Pokemon.srm"
        old_save.write_text("newer save at source")

        new_save = saves_path / "Pokemon.srm"
        new_save.write_text("older stale at destination")

        # Source newer than destination
        os.utime(str(new_save), (1_000_000, 1_000_000))
        os.utime(str(old_save), (2_000_000, 2_000_000))

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        svc, uow = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        result = await svc.migrate_save_sort_files()

        assert result["success"] is True
        assert result["saves_moved"] == 1
        # New file was overwritten with the source contents, old removed.
        assert new_save.read_text() == "newer save at source"
        assert not old_save.exists()
        with uow:
            assert uow.kv_config.get("save_sort_settings_previous") is None

    @pytest.mark.asyncio
    async def test_conflict_mtime_read_oserror_records_error(self, tmp_path):
        """OSError during mtime read — conflict added to errors, no mutations."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"

        rom_file = roms_path / "gba" / "Pokemon.gba"
        old_save = saves_path / "gba" / "Pokemon.srm"
        new_save = saves_path / "Pokemon.srm"

        fake = FakeMigrationFileStore()
        fake.files[str(rom_file)] = b"rom"
        fake.files[str(old_save)] = b"old"
        fake.files[str(new_save)] = b"new"
        # Force the conflict's mtime read to raise on the old (source) path.
        fake.get_mtime_failures.add(str(old_save))

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        svc, uow = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
            migration_file_store=fake,
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        result = await svc.migrate_save_sort_files()

        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert "simulated get_mtime failure" in result["errors"][0]
        # Files untouched — conflict resolution bailed before any mutation.
        assert fake.files[str(old_save)] == b"old"
        assert fake.files[str(new_save)] == b"new"
        # state_previous must be preserved when errors occur — users must still see
        # the migration prompt on the next detect pass
        with uow:
            assert _read_marker(uow, "save_sort_settings_previous") == old_settings

    @pytest.mark.asyncio
    async def test_conflict_remove_oserror_records_error(self, tmp_path):
        """Destination-wins cleanup fails — error recorded, no crash."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"

        rom_file = roms_path / "gba" / "Pokemon.gba"
        old_save = saves_path / "gba" / "Pokemon.srm"
        new_save = saves_path / "Pokemon.srm"

        fake = FakeMigrationFileStore()
        fake.files[str(rom_file)] = b"rom"
        fake.files[str(old_save)] = b"stale"
        fake.files[str(new_save)] = b"fresh"
        # Destination newer than source — newest-wins picks the remove path.
        fake.mtimes[str(old_save)] = 1_000_000.0
        fake.mtimes[str(new_save)] = 2_000_000.0
        fake.remove_failures.add(str(old_save))

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        svc, uow = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
            migration_file_store=fake,
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        result = await svc.migrate_save_sort_files()

        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert "simulated remove failure" in result["errors"][0]
        # state_previous must be preserved when errors occur — users must still see
        # the migration prompt on the next detect pass
        with uow:
            assert _read_marker(uow, "save_sort_settings_previous") == old_settings

    @pytest.mark.asyncio
    async def test_conflict_replace_oserror_records_error(self, tmp_path):
        """Source-wins overwrite fails — error recorded, no crash."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"

        rom_file = roms_path / "gba" / "Pokemon.gba"
        old_save = saves_path / "gba" / "Pokemon.srm"
        new_save = saves_path / "Pokemon.srm"

        fake = FakeMigrationFileStore()
        fake.files[str(rom_file)] = b"rom"
        fake.files[str(old_save)] = b"source newer"
        fake.files[str(new_save)] = b"destination older"
        # Source newer than destination — newest-wins picks the rename path.
        fake.mtimes[str(old_save)] = 2_000_000.0
        fake.mtimes[str(new_save)] = 1_000_000.0
        fake.rename_failures.add(str(old_save))

        installed_roms = {
            "1": {
                "system": "gba",
                "file_path": str(rom_file),
                "platform_slug": "gba",
            }
        }
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        svc, uow = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
            migration_file_store=fake,
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        result = await svc.migrate_save_sort_files()

        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert "simulated rename failure" in result["errors"][0]
        # state_previous must be preserved when errors occur — users must still see
        # the migration prompt on the next detect pass
        with uow:
            assert _read_marker(uow, "save_sort_settings_previous") == old_settings

    @pytest.mark.asyncio
    async def test_clears_previous_on_success(self, tmp_path):
        """After successful migration save_sort_settings_previous is removed from state."""
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": False}
        # No installed ROMs — migration runs with 0 items but still succeeds
        svc, uow = _make_service(
            tmp_path,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))

        result = await svc.migrate_save_sort_files()

        assert result["success"] is True
        with uow:
            assert uow.kv_config.get("save_sort_settings_previous") is None

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
        """Resolver returns (core_so, label); .info lookup returns the
        canonical corename; method returns (corename, core_so) — the
        corename (NOT the label) plus the underlying ``.so`` basename."""

        # Resolver label is "Snes9x - Current" — intentionally different from
        # the RetroArch corename to cover the #208 regression.
        active_core = FakeActiveCoreResolver(default=("snes9x_libretro", "Snes9x - Current"))

        def get_core_name(core_so: str) -> str | None:
            assert core_so == "snes9x_libretro"
            return "Snes9x"

        svc, _ = _make_service(tmp_path, active_core=active_core, get_core_name=get_core_name)
        assert svc._resolve_retroarch_corename(1) == ("Snes9x", "snes9x_libretro")

    def test_active_core_returns_none_returns_none(self, tmp_path):
        """Resolver cannot resolve the active core — method returns (None, None)."""

        active_core = FakeActiveCoreResolver(default=(None, None))

        def get_core_name(core_so: str) -> str | None:
            # Should never be called.
            raise AssertionError("get_core_name called despite unresolved core")

        svc, _ = _make_service(tmp_path, active_core=active_core, get_core_name=get_core_name)
        assert svc._resolve_retroarch_corename(1) == (None, None)

    def test_core_name_returns_none_returns_none_no_label_fallback(self, tmp_path):
        """The resolver gives us a core_so but the .info lookup fails — method
        returns (None, core_so) so the caller can log the failed core
        (NOT the ES-DE label, which is the old bug)."""

        active_core = FakeActiveCoreResolver(default=("oddcore_libretro", "Some ES-DE Label"))

        def get_core_name(core_so: str) -> str | None:
            return None

        svc, _ = _make_service(tmp_path, active_core=active_core, get_core_name=get_core_name)
        assert svc._resolve_retroarch_corename(1) == (None, "oddcore_libretro")

    def test_core_name_returns_empty_string_returns_none(self, tmp_path):
        """.info has ``corename = ""`` — adapter already coerces to None,
        but we also defend at the service layer with ``or None``."""

        active_core = FakeActiveCoreResolver(default=("blank_libretro", "Blank Label"))

        def get_core_name(core_so: str) -> str | None:
            return ""

        svc, _ = _make_service(tmp_path, active_core=active_core, get_core_name=get_core_name)
        assert svc._resolve_retroarch_corename(1) == (None, "blank_libretro")


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
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": True}

        active_core = FakeActiveCoreResolver(default=("snes9x_libretro", "Snes9x - Current"))

        def get_core_name(core_so: str) -> str | None:
            return "Snes9x"

        svc, uow = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
            active_core=active_core,
            get_core_name=get_core_name,
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))
        with uow:
            installs = list(uow.rom_installs.iter_all())

        items = svc._collect_save_sorting_items(old_settings, new_settings, installs)

        # One item produced, destination path contains "Snes9x" (not "Snes9x - Current")
        assert len(items) == 1
        _label, _old_path, new_path, _updater, _kind = items[0]
        assert os.sep + "Snes9x" + os.sep in new_path
        assert "Snes9x - Current" not in new_path

    def test_migration_dest_differs_by_per_game_override(self, tmp_path):
        """RESULT-FLIP: two snes ROMs, one pinned + one default, migrate to different subdirs.

        sort_by_core migration names each ROM's destination after its active
        core's ``.info`` corename. The pinned ROM resolves to Supafaust → ``/Supafaust``;
        the NULL ROM resolves to the default Snes9x → ``/Snes9x``. The destination
        flips on the per-game override alone, keyed by rom_id — proving migration
        sources the per-game core from the resolver, not a platform default.
        """
        roms_path = tmp_path / "roms"
        saves_path = tmp_path / "saves"
        roms_path.mkdir()
        saves_path.mkdir()

        for name in ("Pinned", "Plain"):
            rom_file = roms_path / "snes" / f"{name}.sfc"
            rom_file.parent.mkdir(parents=True, exist_ok=True)
            rom_file.write_text("rom")
            save_dir = saves_path / "snes"
            save_dir.mkdir(parents=True, exist_ok=True)
            (save_dir / f"{name}.srm").write_text("save")

        installed_roms = {
            "1": {"system": "snes", "file_path": str(roms_path / "snes" / "Pinned.sfc"), "platform_slug": "snes"},
            "2": {"system": "snes", "file_path": str(roms_path / "snes" / "Plain.sfc"), "platform_slug": "snes"},
        }
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": True}

        active_core = FakeActiveCoreResolver(
            default=("snes9x_libretro", "Snes9x"),
            per_rom={1: ("supafaust_libretro", "Supafaust")},
        )

        def get_core_name(core_so: str) -> str | None:
            return "Supafaust" if core_so == "supafaust_libretro" else "Snes9x"

        svc, uow = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
            active_core=active_core,
            get_core_name=get_core_name,
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))
        with uow:
            installs = list(uow.rom_installs.iter_all())

        items = svc._collect_save_sorting_items(old_settings, new_settings, installs)

        # The destination subdir flips on the per-game override (old_path carries
        # the source ROM name so each item is attributable to its ROM).
        assert len(items) == 2
        pinned_dest = next(new_path for _l, old_path, new_path, _u, _k in items if "Pinned" in old_path)
        plain_dest = next(new_path for _l, old_path, new_path, _u, _k in items if "Plain" in old_path)
        assert os.sep + "Supafaust" + os.sep in pinned_dest
        assert os.sep + "Snes9x" + os.sep in plain_dest
        assert sorted(active_core.calls) == [1, 2]

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
        old_settings: SaveSortSettings = {"sort_by_content": True, "sort_by_core": False}
        new_settings: SaveSortSettings = {"sort_by_content": False, "sort_by_core": True}

        active_core = FakeActiveCoreResolver(default=("oddcore_libretro", "Oddcore Label"))

        def get_core_name(core_so: str) -> str | None:
            return None

        svc, uow = _make_service(
            tmp_path,
            installed_roms=installed_roms,
            state_overrides={
                "save_sort_settings_previous": old_settings,
                "save_sort_settings": new_settings,
            },
            active_core=active_core,
            get_core_name=get_core_name,
        )
        svc._retrodeck_paths = FakeRetroDeckPaths(saves=str(saves_path), roms=str(roms_path))
        with uow:
            installs = list(uow.rom_installs.iter_all())

        with caplog.at_level(logging.WARNING):
            items = svc._collect_save_sorting_items(old_settings, new_settings, installs)

        assert items == []
        assert any("unable to resolve RetroArch corename" in rec.getMessage() for rec in caplog.records), (
            "Expected a warning about unresolved corename"
        )
        assert any("core_so=oddcore_libretro" in rec.getMessage() for rec in caplog.records), (
            "Expected the warning to include core_so for diagnostics"
        )
