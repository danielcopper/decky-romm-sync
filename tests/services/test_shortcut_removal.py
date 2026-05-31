"""Tests for ShortcutRemovalService."""

import asyncio
import json
from unittest.mock import MagicMock

# conftest.py patches decky before this import
import decky
import pytest
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from adapters.steam_config import SteamConfigAdapter
from domain.rom import Rom
from services.shortcut_removal import ShortcutRemovalService, ShortcutRemovalServiceConfig

_PLATFORM_NAMES_KEY = "platform_names"


def _seed_rom(uow, rom_id, *, app_id, platform_slug="n64", name="Game", cover_path=None):
    """Insert a bound (app_id set) or unbound (app_id None) ROM into the fake UoW."""
    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        fs_name=f"{name}.z64",
        shortcut_app_id=app_id,
        last_synced_at="2025-01-01T00:00:00",
        cover_path=cover_path,
    )
    with uow:
        uow.roms.save(rom)


def _seed_platform_names(uow, mapping):
    with uow:
        uow.kv_config.set(_PLATFORM_NAMES_KEY, json.dumps(mapping))


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def uow_factory(uow) -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory(uow)


@pytest.fixture
def steam_config():
    return SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)


@pytest.fixture
def artwork_remover_mock():
    return MagicMock()


@pytest.fixture
def svc(steam_config, artwork_remover_mock, uow_factory):
    return ShortcutRemovalService(
        config=ShortcutRemovalServiceConfig(
            steam_config=steam_config,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            artwork_remover=artwork_remover_mock,
            uow_factory=uow_factory,
        ),
    )


@pytest.fixture(autouse=True)
async def _set_event_loop(svc):
    svc._loop = asyncio.get_event_loop()


# ── TestRemoveAllShortcuts ────────────────────────────────────────────────────


class TestRemoveAllShortcuts:
    def test_returns_app_ids_and_rom_ids(self, svc, uow):
        _seed_rom(uow, 10, app_id=1001, name="Game A")
        _seed_rom(uow, 20, app_id=1002, name="Game B")
        _seed_rom(uow, 30, app_id=None, name="Game C")  # unbound — no Steam app

        result = svc.remove_all_shortcuts()
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert set(result["rom_ids"]) == {"10", "20", "30"}

    def test_empty_registry(self, svc):
        result = svc.remove_all_shortcuts()
        assert result["success"] is True
        assert result["app_ids"] == []
        assert result["rom_ids"] == []

    def test_does_not_unbind_roms(self, svc, uow):
        """remove_all_shortcuts just returns data; unbinding happens in report_removal_results."""
        _seed_rom(uow, 10, app_id=1001, name="Game A")
        svc.remove_all_shortcuts()
        with uow:
            assert uow.roms.get(10).shortcut_app_id == 1001


# ── TestRemovePlatformShortcuts ───────────────────────────────────────────────


class TestRemovePlatformShortcuts:
    @pytest.mark.asyncio
    async def test_returns_matching_platform_entries(self, svc, uow):
        _seed_platform_names(uow, {"n64": "Nintendo 64", "snes": "Super Nintendo"})
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")
        _seed_rom(uow, 20, app_id=1002, platform_slug="n64", name="Zelda OOT")
        _seed_rom(uow, 30, app_id=1003, platform_slug="snes", name="DKC")

        result = await svc.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert set(result["rom_ids"]) == {"10", "20"}
        assert result["platform_name"] == "Nintendo 64"

    @pytest.mark.asyncio
    async def test_excludes_unbound_rows(self, svc, uow):
        """Unbound (NULL shortcut_app_id) ROMs carry no Steam app id."""
        _seed_platform_names(uow, {"n64": "Nintendo 64"})
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")
        _seed_rom(uow, 20, app_id=None, platform_slug="n64", name="Unbound")

        result = await svc.remove_platform_shortcuts("n64")
        assert result["app_ids"] == [1001]
        assert set(result["rom_ids"]) == {"10", "20"}

    @pytest.mark.asyncio
    async def test_platform_with_no_roms(self, svc, uow):
        """A slug with no synced ROMs returns empty sets and degrades the name to the slug."""
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")

        result = await svc.remove_platform_shortcuts("gba")
        assert result["success"] is True
        assert result["app_ids"] == []
        assert result["rom_ids"] == []
        assert result["platform_name"] == "gba"

    @pytest.mark.asyncio
    async def test_degrades_to_slug_when_name_cache_missing(self, svc, uow):
        """Offline (no cached name) → display name falls back to the bare slug."""
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")

        result = await svc.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert result["app_ids"] == [1001]
        assert result["platform_name"] == "n64"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("blob", ["not json at all {", '"a json string, not a dict"', "[1, 2, 3]"])
    async def test_degrades_to_slug_when_name_cache_corrupt(self, svc, uow, blob):
        """A corrupt / non-dict ``platform_names`` blob decodes to ``{}`` so the
        display name degrades to the slug (bad-path for the decode guard)."""
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")
        with uow:
            uow.kv_config.set(_PLATFORM_NAMES_KEY, blob)

        result = await svc.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert result["app_ids"] == [1001]
        assert result["platform_name"] == "n64"

    @pytest.mark.asyncio
    async def test_does_not_unbind_roms(self, svc, uow):
        """remove_platform_shortcuts just returns data; unbinding happens in report_removal_results."""
        _seed_rom(uow, 10, app_id=1001, platform_slug="n64", name="Mario 64")

        await svc.remove_platform_shortcuts("n64")
        with uow:
            assert uow.roms.get(10).shortcut_app_id == 1001

    @pytest.mark.asyncio
    async def test_handles_exception(self, svc):
        """Exception while resolving the platform set returns the canonical failure shape."""
        mock_loop = MagicMock()
        mock_loop.run_in_executor = MagicMock(side_effect=Exception("boom"))
        svc._loop = mock_loop

        result = await svc.remove_platform_shortcuts("n64")
        assert result["success"] is False
        assert "boom" in result["message"]
        assert result["app_ids"] == []
        assert result["rom_ids"] == []


# ── TestReportRemovalResults ──────────────────────────────────────────────────


class TestReportRemovalResults:
    @pytest.mark.asyncio
    async def test_unbinds_removed_roms_but_keeps_rows(self, svc, uow):
        _seed_rom(uow, 10, app_id=1001, name="Game A")
        _seed_rom(uow, 20, app_id=1002, name="Game B")

        result = await svc.report_removal_results([10, 20])
        assert result["success"] is True
        with uow:
            rom10 = uow.roms.get(10)
            rom20 = uow.roms.get(20)
        # Rows survive (ADR-0007), only the Steam link is cleared.
        assert rom10 is not None and rom10.shortcut_app_id is None
        assert rom20 is not None and rom20.shortcut_app_id is None
        assert uow.committed is True

    @pytest.mark.asyncio
    async def test_partial_removal_leaves_others_bound(self, svc, uow):
        _seed_rom(uow, 10, app_id=1001, name="Game A")
        _seed_rom(uow, 20, app_id=1002, name="Game B")

        await svc.report_removal_results([10])
        with uow:
            assert uow.roms.get(10).shortcut_app_id is None
            assert uow.roms.get(20).shortcut_app_id == 1002

    @pytest.mark.asyncio
    async def test_already_unbound_rom_is_skipped(self, svc, uow):
        """A NULL-app_id row is left untouched (no Steam Input reset, no re-save)."""
        _seed_rom(uow, 10, app_id=None, name="Already Unbound")

        result = await svc.report_removal_results([10])
        assert result["success"] is True
        with uow:
            assert uow.roms.get(10).shortcut_app_id is None

    @pytest.mark.asyncio
    async def test_missing_rom_is_skipped(self, svc, uow):
        """A rom_id with no row in SQLite is ignored, not an error."""
        _seed_rom(uow, 10, app_id=1001, name="Game A")

        result = await svc.report_removal_results([99])
        assert result["success"] is True
        with uow:
            assert uow.roms.get(10).shortcut_app_id == 1001

    @pytest.mark.asyncio
    async def test_cleans_up_artwork_via_callback(self, svc, uow, steam_config, artwork_remover_mock, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        steam_config.grid_dir = lambda: str(grid_dir)
        _seed_rom(uow, 10, app_id=1001, name="Game A", cover_path="/covers/10.png")

        await svc.report_removal_results([10])
        artwork_remover_mock.remove_artwork_files.assert_called_once()
        call = artwork_remover_mock.remove_artwork_files.call_args
        assert call.args[0] == str(grid_dir)
        assert call.args[1] == 10
        assert call.args[2]["cover_path"] == "/covers/10.png"
        assert call.args[2]["app_id"] == 1001


# ── TestRemovalCleansUpArtwork ────────────────────────────────────────────────


class TestRemovalCleansUpArtwork:
    """Integration: report_removal_results drives the real ArtworkService remover."""

    @pytest.mark.asyncio
    async def test_removes_app_id_artwork(self, uow, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        art_file = grid_dir / "100001p.png"
        art_file.write_text("fake")

        _seed_rom(uow, 10, app_id=100001, name="Game A")
        steam_config.grid_dir = lambda: str(grid_dir)

        svc = _artwork_integration_service(uow, steam_config)
        await svc.report_removal_results([10])
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_removes_staging_leftover(self, uow, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        staging = grid_dir / "romm_10_cover.png"
        staging.write_text("fake")

        _seed_rom(uow, 10, app_id=100001, name="Game A")
        steam_config.grid_dir = lambda: str(grid_dir)

        svc = _artwork_integration_service(uow, steam_config)
        await svc.report_removal_results([10])
        assert not staging.exists()


def _artwork_integration_service(uow, steam_config) -> ShortcutRemovalService:
    """Wire a ShortcutRemovalService backed by the real ArtworkService remover."""
    from fakes.fake_unit_of_work import FakeUnitOfWorkFactory

    from adapters.cover_art_file_store import CoverArtFileStoreAdapter
    from services.artwork import ArtworkService, ArtworkServiceConfig

    artwork_svc = ArtworkService(
        config=ArtworkServiceConfig(
            romm_api=MagicMock(),
            steam_config=steam_config,
            cover_art_file_store=CoverArtFileStoreAdapter(),
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            get_pending_sync=dict,
            uow_factory=FakeUnitOfWorkFactory(uow),
        ),
    )
    svc = ShortcutRemovalService(
        config=ShortcutRemovalServiceConfig(
            steam_config=steam_config,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            artwork_remover=artwork_svc,
            uow_factory=FakeUnitOfWorkFactory(uow),
        ),
    )
    svc._loop = asyncio.get_event_loop()
    return svc


# ── TestReportRemovalSteamInputCleanup ────────────────────────────────────────


class TestReportRemovalSteamInputCleanup:
    @pytest.mark.asyncio
    async def test_cleans_up_steam_input_config(self, svc, uow, steam_config):
        steam_config.grid_dir = lambda: None
        _seed_rom(uow, 10, app_id=1001, name="Game A")

        steam_config.set_steam_input_config = MagicMock()
        await svc.report_removal_results([10])
        steam_config.set_steam_input_config.assert_called_once_with([1001], mode="default")

    @pytest.mark.asyncio
    async def test_skips_steam_input_for_unbound_rom(self, svc, uow, steam_config):
        """Unbound rows contribute no app_id, so Steam Input reset is not invoked."""
        steam_config.grid_dir = lambda: None
        _seed_rom(uow, 10, app_id=None, name="Unbound")

        steam_config.set_steam_input_config = MagicMock()
        await svc.report_removal_results([10])
        steam_config.set_steam_input_config.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_steam_input_exception(self, svc, uow, steam_config):
        steam_config.grid_dir = lambda: None
        _seed_rom(uow, 10, app_id=1001, name="Game A")

        steam_config.set_steam_input_config = MagicMock(side_effect=Exception("VDF write failed"))

        # Should not raise, and the ROM is still unbound despite the cleanup failure.
        result = await svc.report_removal_results([10])
        assert result["success"] is True
        with uow:
            assert uow.roms.get(10).shortcut_app_id is None
