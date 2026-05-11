"""Tests for ArtworkService."""

import asyncio
import base64
import logging
import os
from unittest.mock import AsyncMock, MagicMock, patch

# conftest.py patches decky before this import
import decky
import pytest

from adapters.steam_config import SteamConfigAdapter
from services.artwork import ArtworkService


@pytest.fixture
def state():
    return {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}


@pytest.fixture
def steam_config():
    return SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)


@pytest.fixture
def artwork_service(state, steam_config):
    svc = ArtworkService(
        romm_api=MagicMock(),
        steam_config=steam_config,
        state=state,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        emit=decky.emit,
    )
    return svc


@pytest.fixture(autouse=True)
async def _set_event_loop(artwork_service):
    artwork_service._loop = asyncio.get_event_loop()


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _noop_emit_progress(*_args, **_kwargs):
    pass


def _not_cancelling():
    return False


# ── TestExistingCoverPath ─────────────────────────────────────────────────────


class TestExistingCoverPath:
    """Tests for existing_cover_path()."""

    def test_returns_final_when_exists(self, artwork_service, state, tmp_path):
        final = tmp_path / "99999p.png"
        final.write_text("final")
        state["shortcut_registry"]["42"] = {"app_id": 99999}

        result = artwork_service.existing_cover_path(42, str(tmp_path))
        assert result == str(final)

    def test_returns_staging_when_exists(self, artwork_service, tmp_path):
        staging = tmp_path / "romm_42_cover.png"
        staging.write_text("staging")

        result = artwork_service.existing_cover_path(42, str(tmp_path))
        assert result == str(staging)

    def test_returns_none_when_nothing_exists(self, artwork_service, tmp_path):
        result = artwork_service.existing_cover_path(42, str(tmp_path))
        assert result is None

    def test_returns_none_when_registry_no_app_id(self, artwork_service, state, tmp_path):
        state["shortcut_registry"]["42"] = {"name": "Game"}
        result = artwork_service.existing_cover_path(42, str(tmp_path))
        assert result is None


# ── TestDownloadArtwork ───────────────────────────────────────────────────────


class TestDownloadArtwork:
    """Tests for download_artwork()."""

    @pytest.mark.asyncio
    async def test_download_uses_staging_filename(self, artwork_service, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        steam_config.grid_dir = lambda: str(grid_dir)

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock()
        artwork_service._loop = mock_loop

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert 42 in result
        assert result[42].endswith("romm_42_cover.png")
        call_args = mock_loop.run_in_executor.call_args[0]
        assert "romm_42_cover.png" in call_args[3]

    @pytest.mark.asyncio
    async def test_skips_download_if_final_exists(self, artwork_service, state, steam_config, tmp_path):
        """If {app_id}p.png exists from a prior sync, skip re-download."""
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        steam_config.grid_dir = lambda: str(grid_dir)

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock()
        artwork_service._loop = mock_loop

        final_art = grid_dir / "99999p.png"
        final_art.write_text("fake")
        state["shortcut_registry"]["42"] = {"app_id": 99999, "name": "Test"}

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert result[42] == str(final_art)
        mock_loop.run_in_executor.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_download_if_staging_exists(self, artwork_service, steam_config, tmp_path):
        """If staging file exists (e.g. retry), skip re-download."""
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        steam_config.grid_dir = lambda: str(grid_dir)

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock()
        artwork_service._loop = mock_loop

        staging = grid_dir / "romm_42_cover.png"
        staging.write_text("fake")

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert result[42] == str(staging)
        mock_loop.run_in_executor.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_grid_returns_empty(self, artwork_service, steam_config):
        steam_config.grid_dir = lambda: None
        roms = [{"id": 1, "name": "G", "path_cover_large": "/c.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_skips_rom_without_cover_url(self, artwork_service, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        steam_config.grid_dir = lambda: str(grid_dir)

        roms = [{"id": 1, "name": "No Cover"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert 1 not in result

    @pytest.mark.asyncio
    async def test_download_failure_logged(self, artwork_service, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        steam_config.grid_dir = lambda: str(grid_dir)

        mock_loop = MagicMock()
        mock_loop.run_in_executor = AsyncMock(side_effect=Exception("Network error"))
        artwork_service._loop = mock_loop

        roms = [{"id": 1, "name": "Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert 1 not in result

    @pytest.mark.asyncio
    async def test_cancelling_during_artwork(self, artwork_service, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        steam_config.grid_dir = lambda: str(grid_dir)

        roms = [{"id": 1, "name": "Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=lambda: True
        )
        assert result == {}


# ── TestFinalizeCoverPath ─────────────────────────────────────────────────────


class TestFinalizeCoverPath:
    """Tests for finalize_cover_path()."""

    def test_renames_staging_to_final(self, artwork_service, tmp_path):
        grid = str(tmp_path)
        staging = tmp_path / "romm_1_cover.png"
        staging.write_text("cover data")

        result = artwork_service.finalize_cover_path(grid, str(staging), 100001, "1")
        expected = os.path.join(grid, "100001p.png")
        assert result == expected
        assert not staging.exists()
        assert os.path.exists(expected)

    def test_returns_existing_final(self, artwork_service, tmp_path):
        grid = str(tmp_path)
        final = tmp_path / "100001p.png"
        final.write_text("final data")

        result = artwork_service.finalize_cover_path(grid, "/nonexistent/path.png", 100001, "1")
        assert result == str(final)

    def test_returns_cover_path_when_no_grid(self, artwork_service):
        result = artwork_service.finalize_cover_path(None, "/some/path.png", 100001, "1")
        assert result == "/some/path.png"

    def test_returns_cover_path_when_empty(self, artwork_service, tmp_path):
        result = artwork_service.finalize_cover_path(str(tmp_path), "", 100001, "1")
        assert result == ""

    def test_handles_rename_os_error(self, artwork_service, tmp_path):
        grid = str(tmp_path)
        staging = tmp_path / "romm_1_cover.png"
        staging.write_text("data")

        with patch("os.replace", side_effect=OSError("perm denied")):
            result = artwork_service.finalize_cover_path(grid, str(staging), 100001, "1")
        assert result == str(staging)


# ── TestRemoveArtworkFiles ────────────────────────────────────────────────────


class TestRemoveArtworkFiles:
    """Tests for remove_artwork_files()."""

    def test_removes_cover_path(self, artwork_service, tmp_path):
        grid = str(tmp_path)
        cover = tmp_path / "100001p.png"
        cover.write_text("cover data")
        entry = {"cover_path": str(cover), "app_id": 100001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert not cover.exists()

    def test_removes_app_id_fallback(self, artwork_service, tmp_path):
        grid = str(tmp_path)
        art = tmp_path / "100001p.png"
        art.write_text("data")
        entry = {"cover_path": "", "app_id": 100001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert not art.exists()

    def test_removes_legacy_artwork_id(self, artwork_service, tmp_path):
        grid = str(tmp_path)
        art = tmp_path / "12345p.png"
        art.write_text("data")
        entry = {"cover_path": "", "artwork_id": 12345}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert not art.exists()

    def test_removes_staging_leftover(self, artwork_service, tmp_path):
        grid = str(tmp_path)
        staging = tmp_path / "romm_42_cover.png"
        staging.write_text("staging")
        entry = {"cover_path": ""}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert not staging.exists()

    def test_removes_all_types(self, artwork_service, tmp_path):
        grid = str(tmp_path)
        cover = tmp_path / "mycover.png"
        cover.write_text("cover")
        staging = tmp_path / "romm_42_cover.png"
        staging.write_text("staging")
        entry = {"cover_path": str(cover), "app_id": 100001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert not cover.exists()
        assert not staging.exists()


# ── TestGetArtworkBase64 ──────────────────────────────────────────────────────


class TestGetArtworkBase64:
    """Tests for get_artwork_base64()."""

    @pytest.mark.asyncio
    async def test_returns_base64_from_pending(self, artwork_service, steam_config, tmp_path):
        steam_config.grid_dir = lambda: str(tmp_path)

        cover = tmp_path / "romm_42_cover.png"
        cover.write_bytes(b"fake png data")

        pending_sync = {42: {"cover_path": str(cover)}}
        result = await artwork_service.get_artwork_base64(42, pending_sync)
        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"fake png data"

    @pytest.mark.asyncio
    async def test_returns_base64_from_registry(self, artwork_service, state, steam_config, tmp_path):
        steam_config.grid_dir = lambda: str(tmp_path)

        cover = tmp_path / "100001p.png"
        cover.write_bytes(b"registry png")
        state["shortcut_registry"]["42"] = {"cover_path": str(cover)}

        result = await artwork_service.get_artwork_base64(42, {})
        assert result["base64"] is not None

    @pytest.mark.asyncio
    async def test_returns_base64_from_staging_fallback(self, artwork_service, steam_config, tmp_path):
        steam_config.grid_dir = lambda: str(tmp_path)

        staging = tmp_path / "romm_42_cover.png"
        staging.write_bytes(b"staging png")

        result = await artwork_service.get_artwork_base64(42, {})
        assert result["base64"] is not None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_grid(self, artwork_service, steam_config):
        steam_config.grid_dir = lambda: None
        result = await artwork_service.get_artwork_base64(42, {})
        assert result["base64"] is None

    @pytest.mark.asyncio
    async def test_returns_none_when_file_missing(self, artwork_service, steam_config, tmp_path):
        steam_config.grid_dir = lambda: str(tmp_path)
        result = await artwork_service.get_artwork_base64(42, {})
        assert result["base64"] is None


# ── TestIsStagingFileOrphaned ─────────────────────────────────────────────────


class TestIsStagingFileOrphaned:
    """Tests for is_staging_file_orphaned()."""

    def test_orphaned_when_not_in_registry(self, artwork_service, tmp_path):
        result = artwork_service.is_staging_file_orphaned(str(tmp_path), {}, "42")
        assert result is True

    def test_orphaned_when_final_exists(self, artwork_service, tmp_path):
        final = tmp_path / "1001p.png"
        final.write_text("final")
        registry = {"42": {"app_id": 1001}}
        result = artwork_service.is_staging_file_orphaned(str(tmp_path), registry, "42")
        assert result is True

    def test_not_orphaned_when_no_final(self, artwork_service, tmp_path):
        registry = {"42": {"app_id": 1001}}
        result = artwork_service.is_staging_file_orphaned(str(tmp_path), registry, "42")
        assert result is False

    def test_not_orphaned_when_no_app_id(self, artwork_service, tmp_path):
        registry = {"42": {"name": "Game"}}
        result = artwork_service.is_staging_file_orphaned(str(tmp_path), registry, "42")
        assert result is False


# ── TestPruneOrphanedStagingArtwork ──────────────────────────────────────────


class TestPruneOrphanedStagingArtwork:
    """Tests for prune_orphaned_staging_artwork()."""

    def test_removes_staging_not_in_registry(self, artwork_service, state, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        staging = grid_dir / "romm_42_cover.png"
        staging.write_text("fake")

        steam_config.grid_dir = lambda: str(grid_dir)
        state["shortcut_registry"] = {}

        artwork_service.prune_orphaned_staging_artwork()
        assert not staging.exists()

    def test_removes_redundant_staging_with_final(self, artwork_service, state, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        staging = grid_dir / "romm_42_cover.png"
        staging.write_text("fake staging")
        final = grid_dir / "1001p.png"
        final.write_text("fake final")

        steam_config.grid_dir = lambda: str(grid_dir)
        state["shortcut_registry"] = {"42": {"app_id": 1001, "name": "Game A"}}

        artwork_service.prune_orphaned_staging_artwork()
        assert not staging.exists()
        assert final.exists()

    def test_keeps_staging_when_no_final(self, artwork_service, state, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        staging = grid_dir / "romm_42_cover.png"
        staging.write_text("fake staging")

        steam_config.grid_dir = lambda: str(grid_dir)
        state["shortcut_registry"] = {"42": {"app_id": 1001, "name": "Game A"}}

        artwork_service.prune_orphaned_staging_artwork()
        assert staging.exists()

    def test_ignores_non_staging_files(self, artwork_service, state, steam_config, tmp_path):
        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        final = grid_dir / "1001p.png"
        final.write_text("final art")
        other = grid_dir / "something_else.png"
        other.write_text("other")

        steam_config.grid_dir = lambda: str(grid_dir)
        state["shortcut_registry"] = {}

        artwork_service.prune_orphaned_staging_artwork()
        assert final.exists()
        assert other.exists()

    def test_no_grid_dir_no_crash(self, artwork_service, state, steam_config):
        steam_config.grid_dir = lambda: None
        state["shortcut_registry"] = {}
        artwork_service.prune_orphaned_staging_artwork()  # should not raise

    def test_handles_os_error(self, artwork_service, state, steam_config, tmp_path, caplog):

        grid_dir = tmp_path / "grid"
        grid_dir.mkdir()
        staging = grid_dir / "romm_42_cover.png"
        staging.write_text("fake")

        steam_config.grid_dir = lambda: str(grid_dir)
        state["shortcut_registry"] = {}

        with caplog.at_level(logging.WARNING), patch("os.remove", side_effect=OSError("permission denied")):
            artwork_service.prune_orphaned_staging_artwork()

        assert staging.exists()
        assert any("Failed to remove orphaned staging artwork" in r.message for r in caplog.records)
