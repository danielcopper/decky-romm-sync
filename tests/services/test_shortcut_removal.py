"""Tests for ShortcutRemovalService."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

# conftest.py patches decky before this import
import decky
import pytest

from adapters.steam_config import SteamConfigAdapter
from services.shortcut_removal import ShortcutRemovalService, ShortcutRemovalServiceConfig


@pytest.fixture
def state():
    return {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}


@pytest.fixture
def steam_config():
    return SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)


@pytest.fixture
def artwork_remover_mock():
    return MagicMock()


@pytest.fixture
def svc(state, steam_config, artwork_remover_mock):
    service = ShortcutRemovalService(
        config=ShortcutRemovalServiceConfig(
            romm_api=MagicMock(),
            steam_config=steam_config,
            state=state,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            emit=decky.emit,
            save_state=MagicMock(),
            artwork_remover=artwork_remover_mock,
        ),
    )
    return service


@pytest.fixture(autouse=True)
async def _set_event_loop(svc):
    svc._loop = asyncio.get_event_loop()


# ── TestRemoveAllShortcuts ────────────────────────────────────────────────────


class TestRemoveAllShortcuts:
    def test_returns_app_ids_and_rom_ids(self, svc, state):
        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A"},
            "20": {"app_id": 1002, "name": "Game B"},
            "30": {"name": "Game C"},  # no app_id (edge case)
        }
        result = svc.remove_all_shortcuts()
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert set(result["rom_ids"]) == {"10", "20", "30"}

    def test_empty_registry(self, svc):
        result = svc.remove_all_shortcuts()
        assert result["success"] is True
        assert result["app_ids"] == []
        assert result["rom_ids"] == []

    def test_does_not_modify_registry(self, svc, state):
        """remove_all_shortcuts just returns data; registry cleared by report_removal_results."""
        state["shortcut_registry"] = {"10": {"app_id": 1001, "name": "Game A"}}
        svc.remove_all_shortcuts()
        assert "10" in state["shortcut_registry"]


# ── TestRemovePlatformShortcuts ───────────────────────────────────────────────


class TestRemovePlatformShortcuts:
    @pytest.mark.asyncio
    async def test_returns_matching_platform_entries(self, svc, state):
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"id": 1, "slug": "n64", "name": "Nintendo 64"},
                {"id": 2, "slug": "snes", "name": "Super Nintendo"},
            ]
        )
        svc._loop = mock_loop

        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Mario 64", "platform_name": "Nintendo 64"},
            "20": {"app_id": 1002, "name": "Zelda OOT", "platform_name": "Nintendo 64"},
            "30": {"app_id": 1003, "name": "DKC", "platform_name": "Super Nintendo"},
        }

        result = await svc.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert set(result["rom_ids"]) == {"10", "20"}
        assert result["platform_name"] == "Nintendo 64"

    @pytest.mark.asyncio
    async def test_platform_not_found(self, svc):
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=[{"id": 1, "slug": "n64", "name": "Nintendo 64"}])
        svc._loop = mock_loop

        result = await svc.remove_platform_shortcuts("nonexistent")
        assert result["success"] is False
        assert result["app_ids"] == []
        assert result["rom_ids"] == []

    @pytest.mark.asyncio
    async def test_does_not_modify_registry(self, svc, state):
        """remove_platform_shortcuts just returns data; registry cleared by report_removal_results."""
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=[{"id": 1, "slug": "n64", "name": "Nintendo 64"}])
        svc._loop = mock_loop

        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Mario 64", "platform_name": "Nintendo 64"},
        }

        await svc.remove_platform_shortcuts("n64")
        assert "10" in state["shortcut_registry"]

    @pytest.mark.asyncio
    async def test_works_offline_with_registry_slug(self, svc, state):
        """When platform_slug is in the registry, no API call needed."""
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Server unreachable"))
        svc._loop = mock_loop

        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Mario 64", "platform_name": "Nintendo 64", "platform_slug": "n64"},
            "20": {"app_id": 1002, "name": "Zelda OOT", "platform_name": "Nintendo 64", "platform_slug": "n64"},
        }

        result = await svc.remove_platform_shortcuts("n64")
        assert result["success"] is True
        assert set(result["app_ids"]) == {1001, 1002}
        assert result["platform_name"] == "Nintendo 64"

    @pytest.mark.asyncio
    async def test_handles_exception(self, svc):
        """Exception during API call returns failure response."""
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("API Error"))
        svc._loop = mock_loop

        result = await svc.remove_platform_shortcuts("broken")
        assert result["success"] is False
        assert result["app_ids"] == []
        assert result["rom_ids"] == []


# ── TestReportRemovalResults ──────────────────────────────────────────────────


class TestReportRemovalResults:
    @pytest.mark.asyncio
    async def test_removes_entries_from_registry(self, svc, state, tmp_path):
        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "cover_path": ""},
            "20": {"app_id": 1002, "name": "Game B", "cover_path": ""},
        }

        result = await svc.report_removal_results([10, 20])
        assert result["success"] is True
        assert state["shortcut_registry"] == {}

    @pytest.mark.asyncio
    async def test_cleans_up_artwork_via_callback(self, svc, state, steam_config, artwork_remover_mock, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        steam_config.grid_dir = lambda: str(grid_dir)

        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "cover_path": ""},
        }

        await svc.report_removal_results([10])
        assert artwork_remover_mock.remove_artwork_files.called

    @pytest.mark.asyncio
    async def test_partial_removal(self, svc, state, tmp_path):
        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "cover_path": ""},
            "20": {"app_id": 1002, "name": "Game B", "cover_path": ""},
        }

        result = await svc.report_removal_results([10])
        assert result["success"] is True
        assert "10" not in state["shortcut_registry"]
        assert "20" in state["shortcut_registry"]

    @pytest.mark.asyncio
    async def test_updates_sync_stats(self, svc, state, tmp_path):
        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A", "platform_name": "N64", "cover_path": ""},
            "20": {"app_id": 1002, "name": "Game B", "platform_name": "SNES", "cover_path": ""},
        }

        await svc.report_removal_results([10, 20])
        assert state["sync_stats"]["platforms"] == 0
        assert state["sync_stats"]["roms"] == 0


# ── TestRemovalCleansUpArtwork ────────────────────────────────────────────────


class TestRemovalCleansUpArtwork:
    """Tests for artwork cleanup via callback in report_removal_results."""

    @pytest.mark.asyncio
    async def test_removes_app_id_artwork(self, state, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        art_file = grid_dir / "100001p.png"
        art_file.write_text("fake")

        state["shortcut_registry"] = {"10": {"app_id": 100001, "name": "Game A", "cover_path": ""}}
        steam_config.grid_dir = lambda: str(grid_dir)

        from adapters.cover_art_file_store import CoverArtFileStoreAdapter
        from services.artwork import ArtworkService, ArtworkServiceConfig

        artwork_svc = ArtworkService(
            config=ArtworkServiceConfig(
                romm_api=MagicMock(),
                steam_config=steam_config,
                cover_art_file_store=CoverArtFileStoreAdapter(),
                state=state,
                loop=asyncio.get_event_loop(),
                logger=decky.logger,
                get_pending_sync=dict,
            ),
        )

        svc = ShortcutRemovalService(
            config=ShortcutRemovalServiceConfig(
                romm_api=MagicMock(),
                steam_config=steam_config,
                state=state,
                loop=asyncio.get_event_loop(),
                logger=decky.logger,
                emit=decky.emit,
                save_state=MagicMock(),
                artwork_remover=artwork_svc,
            ),
        )
        svc._loop = asyncio.get_event_loop()

        await svc.report_removal_results([10])
        assert not art_file.exists()

    @pytest.mark.asyncio
    async def test_removes_staging_leftover(self, state, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        staging = grid_dir / "romm_10_cover.png"
        staging.write_text("fake")

        state["shortcut_registry"] = {"10": {"app_id": 100001, "name": "Game A", "cover_path": ""}}
        steam_config.grid_dir = lambda: str(grid_dir)

        from adapters.cover_art_file_store import CoverArtFileStoreAdapter
        from services.artwork import ArtworkService, ArtworkServiceConfig

        artwork_svc = ArtworkService(
            config=ArtworkServiceConfig(
                romm_api=MagicMock(),
                steam_config=steam_config,
                cover_art_file_store=CoverArtFileStoreAdapter(),
                state=state,
                loop=asyncio.get_event_loop(),
                logger=decky.logger,
                get_pending_sync=dict,
            ),
        )

        svc = ShortcutRemovalService(
            config=ShortcutRemovalServiceConfig(
                romm_api=MagicMock(),
                steam_config=steam_config,
                state=state,
                loop=asyncio.get_event_loop(),
                logger=decky.logger,
                emit=decky.emit,
                save_state=MagicMock(),
                artwork_remover=artwork_svc,
            ),
        )
        svc._loop = asyncio.get_event_loop()

        await svc.report_removal_results([10])
        assert not staging.exists()


# ── TestFindPlatformNameInRegistry ────────────────────────────────────────────


class TestFindPlatformNameInRegistry:
    def test_finds_by_slug(self, svc, state):
        state["shortcut_registry"] = {
            "1": {"platform_name": "Nintendo 64", "platform_slug": "n64"},
            "2": {"platform_name": "Super Nintendo", "platform_slug": "snes"},
        }
        result = svc._find_platform_name_in_registry("n64")
        assert result == "Nintendo 64"

    def test_returns_none_when_not_found(self, svc, state):
        state["shortcut_registry"] = {"1": {"platform_name": "Nintendo 64", "platform_slug": "n64"}}
        result = svc._find_platform_name_in_registry("gba")
        assert result is None

    def test_empty_registry(self, svc):
        result = svc._find_platform_name_in_registry("n64")
        assert result is None


# ── TestFindPlatformNameFromApi ───────────────────────────────────────────────


class TestFindPlatformNameFromApi:
    @pytest.mark.asyncio
    async def test_finds_by_slug(self, svc):
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(
            return_value=[
                {"slug": "n64", "name": "Nintendo 64"},
                {"slug": "snes", "name": "Super Nintendo"},
            ]
        )
        svc._loop = mock_loop

        result = await svc._find_platform_name_from_api("snes")
        assert result == "Super Nintendo"

    @pytest.mark.asyncio
    async def test_returns_none_when_not_found(self, svc):
        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(return_value=[{"slug": "n64", "name": "Nintendo 64"}])
        svc._loop = mock_loop

        result = await svc._find_platform_name_from_api("gba")
        assert result is None


# ── TestReportRemovalSteamInputCleanup ────────────────────────────────────────


class TestReportRemovalSteamInputCleanup:
    @pytest.mark.asyncio
    async def test_cleans_up_steam_input_config(self, svc, state, steam_config, tmp_path):
        steam_config.grid_dir = lambda: None

        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A"},
        }

        steam_config.set_steam_input_config = MagicMock()
        await svc.report_removal_results([10])
        steam_config.set_steam_input_config.assert_called_once_with([1001], mode="default")

    @pytest.mark.asyncio
    async def test_handles_steam_input_exception(self, svc, state, steam_config, tmp_path):
        steam_config.grid_dir = lambda: None

        state["shortcut_registry"] = {
            "10": {"app_id": 1001, "name": "Game A"},
        }

        steam_config.set_steam_input_config = MagicMock(side_effect=Exception("VDF write failed"))

        # Should not raise
        result = await svc.report_removal_results([10])
        assert result["success"] is True
