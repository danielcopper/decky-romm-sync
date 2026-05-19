"""Tests for ArtworkService."""

import asyncio
import base64
import logging
import os
from unittest.mock import MagicMock

# conftest.py patches decky before this import
import decky
import pytest
from fakes.fake_cover_art_file_store import FakeCoverArtFileStore
from models.state import make_default_plugin_state

from adapters.registry_store import RegistryStoreAdapter
from services.artwork import ArtworkService, ArtworkServiceConfig


@pytest.fixture
def state():
    return make_default_plugin_state()


@pytest.fixture
def file_store() -> FakeCoverArtFileStore:
    return FakeCoverArtFileStore()


@pytest.fixture
def steam_config():
    """Minimal steam-config stub. grid_dir is overridden per test."""

    cfg = MagicMock()
    cfg.grid_dir = MagicMock(return_value=None)
    return cfg


@pytest.fixture
def romm_api():
    return MagicMock()


@pytest.fixture
def pending_sync_data() -> dict:
    """Mutable pending-sync dict; tests mutate this to stage pending entries."""
    return {}


@pytest.fixture
def registry_store(state) -> RegistryStoreAdapter:
    return RegistryStoreAdapter(state=state, logger=decky.logger)


@pytest.fixture
def state_persister() -> MagicMock:
    p = MagicMock()
    p.save_state = MagicMock(return_value=None)
    return p


@pytest.fixture
def artwork_service(state, steam_config, file_store, romm_api, pending_sync_data, registry_store, state_persister):
    # _loop is replaced by the autouse fixture below for async tests; for
    # sync tests it is never touched, so a MagicMock is fine here.
    return ArtworkService(
        config=ArtworkServiceConfig(
            romm_api=romm_api,
            steam_config=steam_config,
            cover_art_file_store=file_store,
            state=state,
            loop=MagicMock(),
            logger=decky.logger,
            get_pending_sync=lambda: pending_sync_data,
            registry_store=registry_store,
            state_persister=state_persister,
        ),
    )


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

    def test_returns_final_when_exists(self, artwork_service, state, file_store, tmp_path):
        final = os.path.join(str(tmp_path), "99999p.png")
        file_store.files[final] = b"final"
        state["shortcut_registry"]["42"] = {"app_id": 99999}

        result = artwork_service.existing_cover_path(42, str(tmp_path))
        assert result == final

    def test_returns_staging_when_exists(self, artwork_service, file_store, tmp_path):
        staging = os.path.join(str(tmp_path), "romm_42_cover.png")
        file_store.files[staging] = b"staging"

        result = artwork_service.existing_cover_path(42, str(tmp_path))
        assert result == staging

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
    async def test_download_uses_staging_filename(self, artwork_service, steam_config, romm_api, tmp_path):
        grid_dir = tmp_path / "grid"
        steam_config.grid_dir.return_value = str(grid_dir)

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert 42 in result
        assert result[42].endswith("romm_42_cover.png")
        # download_cover called with the cover URL and a staging dest
        romm_api.download_cover.assert_called_once()
        call_args = romm_api.download_cover.call_args[0]
        assert call_args[0] == "/cover.png"
        assert call_args[1].endswith("romm_42_cover.png")

    @pytest.mark.asyncio
    async def test_skips_download_if_final_exists(
        self, artwork_service, state, steam_config, file_store, romm_api, tmp_path
    ):
        """If {app_id}p.png exists from a prior sync, skip re-download."""
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir

        final = os.path.join(grid_dir, "99999p.png")
        file_store.files[final] = b"fake"
        state["shortcut_registry"]["42"] = {"app_id": 99999, "name": "Test"}

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert result[42] == final
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_download_if_staging_exists(
        self, artwork_service, steam_config, file_store, romm_api, tmp_path
    ):
        """If staging file exists (e.g. retry), skip re-download."""
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir

        staging = os.path.join(grid_dir, "romm_42_cover.png")
        file_store.files[staging] = b"fake"

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert result[42] == staging
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_grid_returns_empty(self, artwork_service, steam_config):
        steam_config.grid_dir.return_value = None
        roms = [{"id": 1, "name": "G", "path_cover_large": "/c.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert result == {}

    @pytest.mark.asyncio
    async def test_skips_rom_without_cover_url(self, artwork_service, steam_config, tmp_path):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")

        roms = [{"id": 1, "name": "No Cover"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert 1 not in result

    @pytest.mark.asyncio
    async def test_download_failure_logged(self, artwork_service, steam_config, romm_api, tmp_path):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        romm_api.download_cover.side_effect = Exception("Network error")

        roms = [{"id": 1, "name": "Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert 1 not in result

    @pytest.mark.asyncio
    async def test_cancelling_during_artwork(self, artwork_service, steam_config, tmp_path):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")

        roms = [{"id": 1, "name": "Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=lambda: True
        )
        assert result == {}


# ── TestFinalizeCoverPath ─────────────────────────────────────────────────────


class TestFinalizeCoverPath:
    """Tests for finalize_cover_path()."""

    def test_renames_staging_to_final(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        staging = os.path.join(grid, "romm_1_cover.png")
        file_store.files[staging] = b"cover data"

        result = artwork_service.finalize_cover_path(grid, staging, 100001, "1")
        expected = os.path.join(grid, "100001p.png")
        assert result == expected
        assert staging not in file_store.files
        assert file_store.files[expected] == b"cover data"

    def test_returns_existing_final(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        final = os.path.join(grid, "100001p.png")
        file_store.files[final] = b"final data"

        result = artwork_service.finalize_cover_path(grid, "/nonexistent/path.png", 100001, "1")
        assert result == final

    def test_returns_cover_path_when_no_grid(self, artwork_service):
        result = artwork_service.finalize_cover_path(None, "/some/path.png", 100001, "1")
        assert result == "/some/path.png"

    def test_returns_cover_path_when_empty(self, artwork_service, tmp_path):
        result = artwork_service.finalize_cover_path(str(tmp_path), "", 100001, "1")
        assert result == ""

    def test_handles_rename_os_error(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        staging = os.path.join(grid, "romm_1_cover.png")
        file_store.files[staging] = b"data"

        original_rename = file_store.rename

        def boom(src, dst):
            raise OSError("perm denied")

        file_store.rename = boom  # type: ignore[method-assign]
        try:
            result = artwork_service.finalize_cover_path(grid, staging, 100001, "1")
        finally:
            file_store.rename = original_rename  # type: ignore[method-assign]
        assert result == staging


# ── TestRemoveArtworkFiles ────────────────────────────────────────────────────


class TestRemoveArtworkFiles:
    """Tests for remove_artwork_files()."""

    def test_removes_cover_path(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        cover = os.path.join(grid, "100001p.png")
        file_store.files[cover] = b"cover data"
        entry = {"cover_path": cover, "app_id": 100001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert cover not in file_store.files

    def test_removes_app_id_fallback(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        art = os.path.join(grid, "100001p.png")
        file_store.files[art] = b"data"
        entry = {"cover_path": "", "app_id": 100001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert art not in file_store.files

    def test_removes_legacy_artwork_id(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        art = os.path.join(grid, "12345p.png")
        file_store.files[art] = b"data"
        entry = {"cover_path": "", "artwork_id": 12345}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert art not in file_store.files

    def test_removes_staging_leftover(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        staging = os.path.join(grid, "romm_42_cover.png")
        file_store.files[staging] = b"staging"
        entry = {"cover_path": ""}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert staging not in file_store.files

    def test_removes_all_types(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        cover = os.path.join(grid, "mycover.png")
        file_store.files[cover] = b"cover"
        staging = os.path.join(grid, "romm_42_cover.png")
        file_store.files[staging] = b"staging"
        entry = {"cover_path": cover, "app_id": 100001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert cover not in file_store.files
        assert staging not in file_store.files


# ── TestGetArtworkBase64 ──────────────────────────────────────────────────────


class TestGetArtworkBase64:
    """Tests for get_artwork_base64()."""

    @pytest.mark.asyncio
    async def test_returns_base64_from_pending(
        self, artwork_service, steam_config, file_store, pending_sync_data, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path)

        cover = os.path.join(str(tmp_path), "romm_42_cover.png")
        file_store.files[cover] = b"fake png data"

        pending_sync_data[42] = {"cover_path": cover}
        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"fake png data"

    @pytest.mark.asyncio
    async def test_returns_base64_from_registry(self, artwork_service, state, steam_config, file_store, tmp_path):
        steam_config.grid_dir.return_value = str(tmp_path)

        cover = os.path.join(str(tmp_path), "100001p.png")
        file_store.files[cover] = b"registry png"
        state["shortcut_registry"]["42"] = {"cover_path": cover}

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is not None

    @pytest.mark.asyncio
    async def test_returns_base64_from_staging_fallback(self, artwork_service, steam_config, file_store, tmp_path):
        steam_config.grid_dir.return_value = str(tmp_path)

        staging = os.path.join(str(tmp_path), "romm_42_cover.png")
        file_store.files[staging] = b"staging png"

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is not None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_grid(self, artwork_service, steam_config):
        steam_config.grid_dir.return_value = None
        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is None

    @pytest.mark.asyncio
    async def test_returns_none_when_file_missing(self, artwork_service, steam_config, tmp_path):
        steam_config.grid_dir.return_value = str(tmp_path)
        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is None

    @pytest.mark.asyncio
    async def test_registry_app_id_fallback_when_cover_path_empty(
        self, artwork_service, state, steam_config, file_store, tmp_path
    ):
        """Defensive fallback: cover_path empty but {app_id}p.png exists on disk."""
        steam_config.grid_dir.return_value = str(tmp_path)

        final = os.path.join(str(tmp_path), "999p.png")
        file_store.files[final] = b"PNGDATA"
        state["shortcut_registry"]["42"] = {"app_id": 999, "cover_path": ""}

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] == base64.b64encode(b"PNGDATA").decode("ascii")

    @pytest.mark.asyncio
    async def test_registry_app_id_fallback_when_file_missing(self, artwork_service, state, steam_config, tmp_path):
        """Registry app_id present but {app_id}p.png not on disk → no fallback possible."""
        steam_config.grid_dir.return_value = str(tmp_path)

        state["shortcut_registry"]["42"] = {"app_id": 999, "cover_path": ""}

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is None

    @pytest.mark.asyncio
    async def test_no_fallback_when_registry_lacks_app_id(self, artwork_service, state, steam_config, tmp_path):
        """Registry entry exists but lacks app_id — fallback must not crash or false-positive."""
        steam_config.grid_dir.return_value = str(tmp_path)

        state["shortcut_registry"]["42"] = {"name": "Game", "cover_path": ""}

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is None

    @pytest.mark.asyncio
    async def test_primary_registry_cover_path_still_works(
        self, artwork_service, state, steam_config, file_store, tmp_path
    ):
        """Sanity check: primary registry cover_path lookup is not short-circuited by fallback."""
        steam_config.grid_dir.return_value = str(tmp_path)

        cover = os.path.join(str(tmp_path), "100001p.png")
        file_store.files[cover] = b"primary png"
        # cover_path is set — must be used directly, fallback path should not run.
        state["shortcut_registry"]["42"] = {"app_id": 100001, "cover_path": cover}

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] == base64.b64encode(b"primary png").decode("ascii")

    @pytest.mark.asyncio
    async def test_returns_none_when_read_raises(self, artwork_service, steam_config, file_store, tmp_path, caplog):
        steam_config.grid_dir.return_value = str(tmp_path)

        staging = os.path.join(str(tmp_path), "romm_42_cover.png")
        file_store.files[staging] = b"data"

        def boom(_path: str) -> bytes:
            raise OSError("read failed")

        file_store.read_bytes = boom  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is None
        assert any("Failed to read artwork" in r.message for r in caplog.records)


# ── TestRefreshCover ──────────────────────────────────────────────────────────


class TestRefreshCover:
    """Tests for refresh_cover() — single-ROM artwork repair."""

    @pytest.mark.asyncio
    async def test_happy_path(
        self,
        artwork_service,
        state,
        steam_config,
        file_store,
        romm_api,
        state_persister,
        tmp_path,
    ):
        grid = str(tmp_path)
        steam_config.grid_dir.return_value = grid
        state["shortcut_registry"]["42"] = {
            "app_id": 999,
            "name": "Game",
            "fs_name": "Game",
            "platform_name": "Plat",
            "platform_slug": "plat",
            "cover_path": "",
        }
        romm_api.get_rom.return_value = {"id": 42, "path_cover_large": "/c.png"}

        # download_cover writes the staging file; finalize then renames it.
        def fake_download(_url: str, dest: str) -> None:
            file_store.files[dest] = b"new cover bytes"

        romm_api.download_cover.side_effect = fake_download

        result = await artwork_service.refresh_cover(42)

        expected_final = os.path.join(grid, "999p.png")
        assert result == {"success": True, "message": "Cover refreshed", "cover_path": expected_final}
        # Registry was patched with the final path
        assert state["shortcut_registry"]["42"]["cover_path"] == expected_final
        # save_state was driven once after the patch
        state_persister.save_state.assert_called_once()
        # File was renamed from staging to final
        assert expected_final in file_store.files
        assert file_store.files[expected_final] == b"new cover bytes"

    @pytest.mark.asyncio
    async def test_not_synced_when_registry_missing(
        self,
        artwork_service,
        state_persister,
        romm_api,
    ):
        result = await artwork_service.refresh_cover(42)
        assert result == {
            "success": False,
            "reason": "not_synced",
            "message": "ROM is not synced to Steam",
        }
        state_persister.save_state.assert_not_called()
        romm_api.get_rom.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_synced_when_registry_lacks_app_id(
        self,
        artwork_service,
        state,
        state_persister,
        romm_api,
    ):
        state["shortcut_registry"]["42"] = {"name": "No app_id yet"}
        result = await artwork_service.refresh_cover(42)
        assert result["success"] is False
        assert result["reason"] == "not_synced"
        state_persister.save_state.assert_not_called()
        romm_api.get_rom.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_grid_dir(
        self,
        artwork_service,
        state,
        steam_config,
        state_persister,
        romm_api,
    ):
        state["shortcut_registry"]["42"] = {"app_id": 999}
        steam_config.grid_dir.return_value = None

        result = await artwork_service.refresh_cover(42)
        assert result == {
            "success": False,
            "reason": "no_grid_dir",
            "message": "Steam grid directory not found",
        }
        state_persister.save_state.assert_not_called()
        romm_api.get_rom.assert_not_called()

    @pytest.mark.asyncio
    async def test_server_unreachable_when_get_rom_raises(
        self,
        artwork_service,
        state,
        steam_config,
        romm_api,
        state_persister,
        tmp_path,
    ):
        state["shortcut_registry"]["42"] = {"app_id": 999}
        steam_config.grid_dir.return_value = str(tmp_path)
        romm_api.get_rom.side_effect = Exception("network down")

        result = await artwork_service.refresh_cover(42)
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert result["message"] == "Could not fetch ROM from server"
        state_persister.save_state.assert_not_called()
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_server_unreachable_when_get_rom_returns_none(
        self,
        artwork_service,
        state,
        steam_config,
        romm_api,
        state_persister,
        tmp_path,
    ):
        state["shortcut_registry"]["42"] = {"app_id": 999}
        steam_config.grid_dir.return_value = str(tmp_path)
        romm_api.get_rom.return_value = None

        result = await artwork_service.refresh_cover(42)
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        state_persister.save_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_cover_url_in_rom_payload(
        self,
        artwork_service,
        state,
        steam_config,
        romm_api,
        state_persister,
        tmp_path,
    ):
        state["shortcut_registry"]["42"] = {"app_id": 999}
        steam_config.grid_dir.return_value = str(tmp_path)
        romm_api.get_rom.return_value = {"id": 42, "name": "No Cover"}

        result = await artwork_service.refresh_cover(42)
        assert result == {
            "success": False,
            "reason": "no_cover",
            "message": "ROM has no cover artwork",
        }
        state_persister.save_state.assert_not_called()
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_small_cover_url(
        self,
        artwork_service,
        state,
        steam_config,
        file_store,
        romm_api,
        state_persister,
        tmp_path,
    ):
        """``path_cover_small`` is used when ``path_cover_large`` is absent."""
        grid = str(tmp_path)
        steam_config.grid_dir.return_value = grid
        state["shortcut_registry"]["42"] = {"app_id": 999, "cover_path": ""}
        romm_api.get_rom.return_value = {"id": 42, "path_cover_small": "/small.png"}

        def fake_download(_url: str, dest: str) -> None:
            file_store.files[dest] = b"small"

        romm_api.download_cover.side_effect = fake_download

        result = await artwork_service.refresh_cover(42)
        assert result["success"] is True
        # download_cover was called with the small URL
        romm_api.download_cover.assert_called_once()
        assert romm_api.download_cover.call_args[0][0] == "/small.png"
        state_persister.save_state.assert_called_once()

    @pytest.mark.asyncio
    async def test_download_failure_does_not_mutate_registry(
        self,
        artwork_service,
        state,
        steam_config,
        romm_api,
        state_persister,
        tmp_path,
    ):
        """When ``download_cover`` raises, registry and persister must remain untouched."""
        grid = str(tmp_path)
        steam_config.grid_dir.return_value = grid
        state["shortcut_registry"]["42"] = {"app_id": 999, "cover_path": "old/path.png"}
        romm_api.get_rom.return_value = {"id": 42, "path_cover_large": "/c.png"}
        romm_api.download_cover.side_effect = Exception("disk full")

        result = await artwork_service.refresh_cover(42)
        assert result["success"] is False
        assert result["reason"] == "download_failed"
        assert "disk full" in result["message"]
        # Registry cover_path unchanged
        assert state["shortcut_registry"]["42"]["cover_path"] == "old/path.png"
        # No persistence on failure
        state_persister.save_state.assert_not_called()


# ── TestIsStagingFileOrphaned ─────────────────────────────────────────────────


class TestIsStagingFileOrphaned:
    """Tests for is_staging_file_orphaned()."""

    def test_orphaned_when_not_in_registry(self, artwork_service, tmp_path):
        result = artwork_service.is_staging_file_orphaned(str(tmp_path), {}, "42")
        assert result is True

    def test_orphaned_when_final_exists(self, artwork_service, file_store, tmp_path):
        final = os.path.join(str(tmp_path), "1001p.png")
        file_store.files[final] = b"final"
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

    def test_removes_staging_not_in_registry(self, artwork_service, state, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        staging = os.path.join(grid_dir, "romm_42_cover.png")
        file_store.files[staging] = b"fake"

        steam_config.grid_dir.return_value = grid_dir
        state["shortcut_registry"] = {}

        artwork_service.prune_orphaned_staging_artwork()
        assert staging not in file_store.files

    def test_removes_redundant_staging_with_final(self, artwork_service, state, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        staging = os.path.join(grid_dir, "romm_42_cover.png")
        final = os.path.join(grid_dir, "1001p.png")
        file_store.files[staging] = b"fake staging"
        file_store.files[final] = b"fake final"

        steam_config.grid_dir.return_value = grid_dir
        state["shortcut_registry"] = {"42": {"app_id": 1001, "name": "Game A"}}

        artwork_service.prune_orphaned_staging_artwork()
        assert staging not in file_store.files
        assert final in file_store.files

    def test_keeps_staging_when_no_final(self, artwork_service, state, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        staging = os.path.join(grid_dir, "romm_42_cover.png")
        file_store.files[staging] = b"fake staging"

        steam_config.grid_dir.return_value = grid_dir
        state["shortcut_registry"] = {"42": {"app_id": 1001, "name": "Game A"}}

        artwork_service.prune_orphaned_staging_artwork()
        assert staging in file_store.files

    def test_ignores_non_staging_files(self, artwork_service, state, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        final = os.path.join(grid_dir, "1001p.png")
        other = os.path.join(grid_dir, "something_else.png")
        file_store.files[final] = b"final art"
        file_store.files[other] = b"other"

        steam_config.grid_dir.return_value = grid_dir
        state["shortcut_registry"] = {}

        artwork_service.prune_orphaned_staging_artwork()
        assert final in file_store.files
        assert other in file_store.files

    def test_no_grid_dir_no_crash(self, artwork_service, state, steam_config):
        steam_config.grid_dir.return_value = None
        state["shortcut_registry"] = {}
        artwork_service.prune_orphaned_staging_artwork()  # should not raise

    def test_grid_not_a_directory_no_crash(self, artwork_service, state, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        # No files under grid_dir => isdir returns False
        file_store.isdir_paths = set()
        state["shortcut_registry"] = {}
        artwork_service.prune_orphaned_staging_artwork()  # should not raise

    def test_handles_os_error(self, artwork_service, state, steam_config, file_store, tmp_path, caplog):
        grid_dir = str(tmp_path / "grid")
        staging = os.path.join(grid_dir, "romm_42_cover.png")
        file_store.files[staging] = b"fake"

        steam_config.grid_dir.return_value = grid_dir
        state["shortcut_registry"] = {}

        def boom(_path: str) -> None:
            raise OSError("permission denied")

        file_store.remove_file = boom  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            artwork_service.prune_orphaned_staging_artwork()

        assert staging in file_store.files
        assert any("Failed to remove orphaned staging artwork" in r.message for r in caplog.records)
