"""Tests for ArtworkService."""

import asyncio
import base64
import logging
import os
from typing import Any
from unittest.mock import MagicMock

# conftest.py patches decky before this import
import decky
import pytest
from fakes.fake_cover_art_file_store import FakeCoverArtFileStore
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.rom import Rom
from services.artwork import ArtworkService, ArtworkServiceConfig


def _seed_rom(uow, rom_id, *, app_id, cover_path=None, platform_slug="n64", name="Game", sgdb_id=None, group_key=None):
    """Insert a bound (or unbound when app_id is None) ROM into the fake UoW."""
    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        fs_name=f"{name}.z64",
        shortcut_app_id=app_id,
        last_synced_at="2025-01-01T00:00:00",
        cover_path=cover_path,
        sgdb_id=sgdb_id,
        sibling_group_key=group_key,
    )
    with uow:
        uow.roms.save(rom)


def _tmp(path: str) -> str:
    """The atomic-write sidecar path (append ``.tmp``)."""
    return path + ".tmp"


def _writing_download(file_store, payload: bytes = b"downloaded"):
    """A ``download_cover`` side effect that materializes its dest (the ``.tmp`` sidecar).

    The atomic download streams to ``dest.tmp`` then renames it over the cache
    file, so the mock must actually create the dest for the rename to succeed —
    mirroring the real adapter's post-download contract.
    """

    def _dl(_url, dest):
        file_store.files[dest] = payload

    return _dl


@pytest.fixture
def cover_cache_dir(tmp_path) -> str:
    """The plugin-owned per-ROM cover cache directory (distinct from the grid)."""
    return str(tmp_path / "covers")


def _cache(cover_cache_dir: str, rom_id: int) -> str:
    """The cache path for *rom_id* under *cover_cache_dir*."""
    return os.path.join(cover_cache_dir, f"{rom_id}.png")


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
def pending_sync_data() -> dict[str, Any]:
    """Mutable pending-sync dict; tests mutate this to stage pending entries."""
    return {}


@pytest.fixture
def uow() -> FakeUnitOfWork:
    """Shared in-memory UoW the tests seed (``uow.roms``) and assert against."""
    return FakeUnitOfWork()


@pytest.fixture
def artwork_service(steam_config, file_store, romm_api, pending_sync_data, uow, cover_cache_dir):
    # _loop is replaced by the autouse fixture below for async tests; for
    # sync tests it is never touched, so a MagicMock is fine here.
    return ArtworkService(
        config=ArtworkServiceConfig(
            romm_api=romm_api,
            steam_config=steam_config,
            cover_art_file_store=file_store,
            cover_cache_dir=cover_cache_dir,
            loop=MagicMock(),
            logger=decky.logger,
            get_pending_sync=lambda: pending_sync_data,
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
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
    """Tests for existing_cover_path() — the grid-side fallback."""

    def test_returns_final_when_exists(self, artwork_service, uow, file_store, tmp_path):
        final = os.path.join(str(tmp_path), "99999p.png")
        file_store.files[final] = b"final"
        _seed_rom(uow, 42, app_id=99999)

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

    def test_returns_none_when_rom_unbound(self, artwork_service, uow, tmp_path):
        _seed_rom(uow, 42, app_id=None)
        result = artwork_service.existing_cover_path(42, str(tmp_path))
        assert result is None


# ── TestDownloadArtwork ───────────────────────────────────────────────────────


class TestDownloadArtwork:
    """Tests for download_artwork() — downloads into the per-ROM cache."""

    @pytest.mark.asyncio
    async def test_download_uses_cache_filename_via_tmp_sidecar(
        self, artwork_service, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        grid_dir = tmp_path / "grid"
        steam_config.grid_dir.return_value = str(grid_dir)
        romm_api.download_cover.side_effect = _writing_download(file_store)

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        cache = _cache(cover_cache_dir, 42)
        assert result[42] == cache
        # The download streams to the .tmp sidecar; the atomic rename publishes it
        # onto the final cache path and leaves no sidecar behind.
        romm_api.download_cover.assert_called_once()
        call_args = romm_api.download_cover.call_args[0]
        assert call_args[0] == "/cover.png"
        assert call_args[1] == _tmp(cache)
        assert file_store.files[cache] == b"downloaded"
        assert _tmp(cache) not in file_store.files

    @pytest.mark.asyncio
    async def test_reuses_existing_cache_file(
        self, artwork_service, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A pre-existing cache file short-circuits the download."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cached"

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert result[42] == cache
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_seeds_cache_from_existing_grid_cover(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A released single-version install ({app_id}p.png present) seeds the
        cache by copying grid→cache rather than re-downloading."""
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        final = os.path.join(grid_dir, "99999p.png")
        file_store.files[final] = b"grid cover"
        _seed_rom(uow, 42, app_id=99999, name="Test")

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        cache = _cache(cover_cache_dir, 42)
        assert result[42] == cache
        # The grid cover was copied into the cache; the grid file survives.
        assert file_store.files[cache] == b"grid cover"
        assert file_store.files[final] == b"grid cover"
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_downloads_when_seed_copy_fails(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A failed grid→cache seed copy falls through to a fresh download."""
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        final = os.path.join(grid_dir, "99999p.png")
        file_store.files[final] = b"grid cover"
        file_store.copy_failures.add(final)
        romm_api.download_cover.side_effect = _writing_download(file_store)
        _seed_rom(uow, 42, app_id=99999, name="Test")

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert result[42] == _cache(cover_cache_dir, 42)
        romm_api.download_cover.assert_called_once()

    @pytest.mark.asyncio
    async def test_multi_version_group_does_not_seed_downloads_instead(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A multi-version sibling shares one grid file — seeding would copy the
        wrong version's art, so an empty-cache member downloads its own instead."""
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        # Rom 2 is bound with A's art on the shared grid file; rom 3 is its sibling.
        final = os.path.join(grid_dir, "99999p.png")
        file_store.files[final] = b"OTHER VERSION art"
        _seed_rom(uow, 2, app_id=99999, name="B", group_key="igdb:5:57")
        _seed_rom(uow, 3, app_id=None, name="C", group_key="igdb:5:57")
        romm_api.download_cover.side_effect = _writing_download(file_store, b"B's own art")

        roms = [{"id": 2, "name": "B", "path_cover_large": "/b.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        cache = _cache(cover_cache_dir, 2)
        assert result[2] == cache
        # The grid's foreign art was NOT copied into the cache — a fresh download won.
        romm_api.download_cover.assert_called_once()
        assert file_store.files[cache] == b"B's own art"
        assert file_store.files[final] == b"OTHER VERSION art"  # grid untouched

    @pytest.mark.asyncio
    async def test_single_version_with_derived_group_key_still_seeds(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A synced single carries a derived (unique) group key — group size is 1,
        so the grid→cache seed still applies (no re-download)."""
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        final = os.path.join(grid_dir, "99999p.png")
        file_store.files[final] = b"grid cover"
        _seed_rom(uow, 42, app_id=99999, name="Solo", group_key="romm:42:57")

        roms = [{"id": 42, "name": "Solo", "path_cover_large": "/cover.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        cache = _cache(cover_cache_dir, 42)
        assert result[42] == cache
        assert file_store.files[cache] == b"grid cover"
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
    """Tests for finalize_cover_path() — copies cache→grid, cache survives."""

    def test_copies_cache_to_final_and_keeps_cache(self, artwork_service, file_store, cover_cache_dir, tmp_path):
        grid = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 1)
        file_store.files[cache] = b"cover data"

        result = artwork_service.finalize_cover_path(grid, cache, 100001, "1")
        expected_final = os.path.join(grid, "100001p.png")
        # The persisted path is the CACHE path; the grid gets a COPY (cache survives).
        assert result == cache
        assert file_store.files[cache] == b"cover data"
        assert file_store.files[expected_final] == b"cover data"

    def test_returns_existing_final_when_cover_missing(self, artwork_service, file_store, tmp_path):
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

    def test_handles_copy_os_error(self, artwork_service, file_store, cover_cache_dir, tmp_path):
        grid = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 1)
        file_store.files[cache] = b"data"
        file_store.copy_failures.add(cache)

        result = artwork_service.finalize_cover_path(grid, cache, 100001, "1")
        # A failed copy still returns the cache path (persist unchanged) and leaves it in place.
        assert result == cache
        assert file_store.files[cache] == b"data"
        assert os.path.join(grid, "100001p.png") not in file_store.files


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

    def test_removes_cache_file(self, artwork_service, file_store, cover_cache_dir, tmp_path):
        grid = str(tmp_path)
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cache cover"
        entry = {"cover_path": "", "app_id": 100001}
        artwork_service.remove_artwork_files(grid, 42, entry)
        assert cache not in file_store.files

    def test_removes_all_types(self, artwork_service, file_store, cover_cache_dir, tmp_path):
        grid = str(tmp_path)
        cover = os.path.join(grid, "mycover.png")
        file_store.files[cover] = b"cover"
        staging = os.path.join(grid, "romm_42_cover.png")
        file_store.files[staging] = b"staging"
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cache"
        entry = {"cover_path": cover, "app_id": 100001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert cover not in file_store.files
        assert staging not in file_store.files
        assert cache not in file_store.files


# ── TestGetArtworkBase64 ──────────────────────────────────────────────────────


class TestGetArtworkBase64:
    """Tests for get_artwork_base64()."""

    @pytest.mark.asyncio
    async def test_returns_base64_from_pending(
        self, artwork_service, steam_config, file_store, pending_sync_data, cover_cache_dir, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path)

        cover = _cache(cover_cache_dir, 42)
        file_store.files[cover] = b"fake png data"

        pending_sync_data[42] = {"cover_path": cover}
        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is not None
        assert base64.b64decode(result["base64"]) == b"fake png data"

    @pytest.mark.asyncio
    async def test_returns_base64_from_rom_cover_path_cache(
        self, artwork_service, uow, steam_config, file_store, cover_cache_dir, tmp_path
    ):
        """A ROM row whose persisted cover_path is a cache path is read directly."""
        steam_config.grid_dir.return_value = str(tmp_path)
        cover = _cache(cover_cache_dir, 42)
        file_store.files[cover] = b"cache png"
        _seed_rom(uow, 42, app_id=100001, cover_path=cover)

        result = await artwork_service.get_artwork_base64(42)
        assert base64.b64decode(result["base64"]) == b"cache png"

    @pytest.mark.asyncio
    async def test_returns_base64_from_legacy_grid_cover_path(
        self, artwork_service, uow, steam_config, file_store, tmp_path
    ):
        """A legacy row whose cover_path points at the grid {app_id}p.png still resolves."""
        steam_config.grid_dir.return_value = str(tmp_path)
        cover = os.path.join(str(tmp_path), "100001p.png")
        file_store.files[cover] = b"legacy grid png"
        _seed_rom(uow, 42, app_id=100001, cover_path=cover)

        result = await artwork_service.get_artwork_base64(42)
        assert base64.b64decode(result["base64"]) == b"legacy grid png"

    @pytest.mark.asyncio
    async def test_returns_base64_from_cache_when_cover_path_empty(
        self, artwork_service, uow, steam_config, file_store, cover_cache_dir, tmp_path
    ):
        """No pending / persisted path, but the per-ROM cache file exists."""
        steam_config.grid_dir.return_value = str(tmp_path)
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cache only"
        _seed_rom(uow, 42, app_id=100001, cover_path="")

        result = await artwork_service.get_artwork_base64(42)
        assert base64.b64decode(result["base64"]) == b"cache only"

    @pytest.mark.asyncio
    async def test_returns_base64_from_cache_without_grid(
        self, artwork_service, steam_config, file_store, cover_cache_dir
    ):
        """The cache lookup does not depend on the Steam grid dir (offline-safe)."""
        steam_config.grid_dir.return_value = None
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cache offline"

        result = await artwork_service.get_artwork_base64(42)
        assert base64.b64decode(result["base64"]) == b"cache offline"

    @pytest.mark.asyncio
    async def test_returns_base64_from_staging_fallback(self, artwork_service, steam_config, file_store, tmp_path):
        steam_config.grid_dir.return_value = str(tmp_path)

        staging = os.path.join(str(tmp_path), "romm_42_cover.png")
        file_store.files[staging] = b"staging png"

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is not None

    @pytest.mark.asyncio
    async def test_returns_none_when_no_grid_and_no_cache(self, artwork_service, steam_config):
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
        self, artwork_service, uow, steam_config, file_store, tmp_path
    ):
        """Defensive fallback: cover_path empty but {app_id}p.png exists on disk."""
        steam_config.grid_dir.return_value = str(tmp_path)

        final = os.path.join(str(tmp_path), "999p.png")
        file_store.files[final] = b"PNGDATA"
        _seed_rom(uow, 42, app_id=999, cover_path="")

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] == base64.b64encode(b"PNGDATA").decode("ascii")

    @pytest.mark.asyncio
    async def test_registry_app_id_fallback_when_file_missing(self, artwork_service, uow, steam_config, tmp_path):
        """ROM app_id present but {app_id}p.png not on disk → no fallback possible."""
        steam_config.grid_dir.return_value = str(tmp_path)

        _seed_rom(uow, 42, app_id=999, cover_path="")

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is None

    @pytest.mark.asyncio
    async def test_no_fallback_when_rom_unbound(self, artwork_service, uow, steam_config, tmp_path):
        """Unbound ROM (no app_id) — fallback must not crash or false-positive."""
        steam_config.grid_dir.return_value = str(tmp_path)

        _seed_rom(uow, 42, app_id=None, cover_path="")

        result = await artwork_service.get_artwork_base64(42)
        assert result["base64"] is None

    @pytest.mark.asyncio
    async def test_primary_rom_cover_path_still_works(self, artwork_service, uow, steam_config, file_store, tmp_path):
        """Sanity check: primary ROM cover_path lookup is not short-circuited by fallback."""
        steam_config.grid_dir.return_value = str(tmp_path)

        cover = os.path.join(str(tmp_path), "100001p.png")
        file_store.files[cover] = b"primary png"
        # cover_path is set — must be used directly, fallback path should not run.
        _seed_rom(uow, 42, app_id=100001, cover_path=cover)

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


# ── TestFetchCoverBase64 ──────────────────────────────────────────────────────


class TestFetchCoverBase64:
    """Tests for fetch_cover_base64() — cache-first, RomM on miss, never fails loudly."""

    @pytest.mark.asyncio
    async def test_cache_hit_returns_bytes_without_network(
        self, artwork_service, file_store, romm_api, cover_cache_dir
    ):
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cached cover"

        result = await artwork_service.fetch_cover_base64(42)
        assert base64.b64decode(result["base64"]) == b"cached cover"
        romm_api.get_rom.assert_not_called()
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_miss_downloads_from_romm_into_cache(self, artwork_service, file_store, romm_api, cover_cache_dir):
        romm_api.get_rom.return_value = {"id": 42, "path_cover_large": "/c.png"}

        def fake_download(_url: str, dest: str) -> None:
            file_store.files[dest] = b"downloaded cover"

        romm_api.download_cover.side_effect = fake_download

        result = await artwork_service.fetch_cover_base64(42)
        cache = _cache(cover_cache_dir, 42)
        assert base64.b64decode(result["base64"]) == b"downloaded cover"
        assert file_store.files[cache] == b"downloaded cover"
        romm_api.download_cover.assert_called_once()
        # The download streams to the .tmp sidecar; the atomic rename publishes it.
        assert romm_api.download_cover.call_args[0] == ("/c.png", _tmp(cache))
        assert _tmp(cache) not in file_store.files

    @pytest.mark.asyncio
    async def test_works_without_local_db_row(self, artwork_service, uow, romm_api, file_store):
        """A group version with no local ``roms`` row still fetches its cover."""
        romm_api.get_rom.return_value = {"id": 77, "path_cover_small": "/s.png"}

        def fake_download(_url: str, dest: str) -> None:
            file_store.files[dest] = b"server-only cover"

        romm_api.download_cover.side_effect = fake_download

        result = await artwork_service.fetch_cover_base64(77)
        assert base64.b64decode(result["base64"]) == b"server-only cover"

    @pytest.mark.asyncio
    async def test_server_unreachable_returns_none(self, artwork_service, romm_api):
        romm_api.get_rom.side_effect = Exception("offline")
        result = await artwork_service.fetch_cover_base64(42)
        assert result == {"base64": None}

    @pytest.mark.asyncio
    async def test_rom_without_cover_returns_none(self, artwork_service, romm_api):
        romm_api.get_rom.return_value = {"id": 42, "name": "No Cover"}
        result = await artwork_service.fetch_cover_base64(42)
        assert result == {"base64": None}
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_rom_returns_none_returns_none(self, artwork_service, romm_api):
        romm_api.get_rom.return_value = None
        result = await artwork_service.fetch_cover_base64(42)
        assert result == {"base64": None}

    @pytest.mark.asyncio
    async def test_download_failure_returns_none(self, artwork_service, romm_api):
        romm_api.get_rom.return_value = {"id": 42, "path_cover_large": "/c.png"}
        romm_api.download_cover.side_effect = Exception("disk full")
        result = await artwork_service.fetch_cover_base64(42)
        assert result == {"base64": None}

    @pytest.mark.asyncio
    async def test_cache_hit_read_error_returns_none(self, artwork_service, file_store, romm_api, cover_cache_dir):
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"data"

        def boom(_path: str) -> bytes:
            raise OSError("read failed")

        file_store.read_bytes = boom  # type: ignore[method-assign]

        result = await artwork_service.fetch_cover_base64(42)
        assert result == {"base64": None}
        romm_api.get_rom.assert_not_called()


# ── TestRefreshCover ──────────────────────────────────────────────────────────


class TestRefreshCover:
    """Tests for refresh_cover() — single-ROM artwork repair."""

    @pytest.mark.asyncio
    async def test_happy_path(
        self,
        artwork_service,
        uow,
        steam_config,
        file_store,
        romm_api,
        cover_cache_dir,
        tmp_path,
    ):
        grid = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid
        _seed_rom(uow, 42, app_id=999, platform_slug="plat", name="Game", cover_path="")
        romm_api.get_rom.return_value = {"id": 42, "path_cover_large": "/c.png"}

        def fake_download(_url: str, dest: str) -> None:
            file_store.files[dest] = b"new cover bytes"

        romm_api.download_cover.side_effect = fake_download

        result = await artwork_service.refresh_cover(42)

        cache = _cache(cover_cache_dir, 42)
        expected_final = os.path.join(grid, "999p.png")
        # The persisted cover_path is the CACHE path; the grid gets a copy.
        assert result == {"success": True, "message": "Cover refreshed", "cover_path": cache}
        with uow:
            assert uow.roms.get(42).cover_path == cache
        assert uow.committed is True
        assert file_store.files[cache] == b"new cover bytes"
        assert file_store.files[expected_final] == b"new cover bytes"

    @pytest.mark.asyncio
    async def test_not_synced_when_rom_missing(
        self,
        artwork_service,
        romm_api,
    ):
        result = await artwork_service.refresh_cover(42)
        assert result == {
            "success": False,
            "reason": "not_synced",
            "message": "ROM is not synced to Steam",
        }
        romm_api.get_rom.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_synced_when_rom_unbound(
        self,
        artwork_service,
        uow,
        romm_api,
    ):
        _seed_rom(uow, 42, app_id=None)
        result = await artwork_service.refresh_cover(42)
        assert result["success"] is False
        assert result["reason"] == "not_synced"
        romm_api.get_rom.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_grid_dir(
        self,
        artwork_service,
        uow,
        steam_config,
        romm_api,
    ):
        _seed_rom(uow, 42, app_id=999)
        steam_config.grid_dir.return_value = None

        result = await artwork_service.refresh_cover(42)
        assert result == {
            "success": False,
            "reason": "no_grid_dir",
            "message": "Steam grid directory not found",
        }
        romm_api.get_rom.assert_not_called()

    @pytest.mark.asyncio
    async def test_server_unreachable_when_get_rom_raises(
        self,
        artwork_service,
        uow,
        steam_config,
        romm_api,
        tmp_path,
    ):
        _seed_rom(uow, 42, app_id=999)
        steam_config.grid_dir.return_value = str(tmp_path)
        romm_api.get_rom.side_effect = Exception("network down")

        result = await artwork_service.refresh_cover(42)
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert result["message"] == "Could not fetch ROM from server"
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_server_unreachable_when_get_rom_returns_none(
        self,
        artwork_service,
        uow,
        steam_config,
        romm_api,
        tmp_path,
    ):
        _seed_rom(uow, 42, app_id=999)
        steam_config.grid_dir.return_value = str(tmp_path)
        romm_api.get_rom.return_value = None

        result = await artwork_service.refresh_cover(42)
        assert result["success"] is False
        assert result["reason"] == "server_unreachable"

    @pytest.mark.asyncio
    async def test_no_cover_url_in_rom_payload(
        self,
        artwork_service,
        uow,
        steam_config,
        romm_api,
        tmp_path,
    ):
        _seed_rom(uow, 42, app_id=999)
        steam_config.grid_dir.return_value = str(tmp_path)
        romm_api.get_rom.return_value = {"id": 42, "name": "No Cover"}

        result = await artwork_service.refresh_cover(42)
        assert result == {
            "success": False,
            "reason": "no_cover",
            "message": "ROM has no cover artwork",
        }
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_falls_back_to_small_cover_url(
        self,
        artwork_service,
        uow,
        steam_config,
        file_store,
        romm_api,
        cover_cache_dir,
        tmp_path,
    ):
        """``path_cover_small`` is used when ``path_cover_large`` is absent."""
        grid = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid
        _seed_rom(uow, 42, app_id=999, cover_path="")
        romm_api.get_rom.return_value = {"id": 42, "path_cover_small": "/small.png"}

        def fake_download(_url: str, dest: str) -> None:
            file_store.files[dest] = b"small"

        romm_api.download_cover.side_effect = fake_download

        result = await artwork_service.refresh_cover(42)
        assert result["success"] is True
        romm_api.download_cover.assert_called_once()
        assert romm_api.download_cover.call_args[0][0] == "/small.png"
        with uow:
            assert uow.roms.get(42).cover_path == _cache(cover_cache_dir, 42)

    @pytest.mark.asyncio
    async def test_download_failure_does_not_mutate_rom(
        self,
        artwork_service,
        uow,
        steam_config,
        romm_api,
        tmp_path,
    ):
        """When ``download_cover`` raises, the ROM row's cover_path must remain untouched."""
        grid = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid
        _seed_rom(uow, 42, app_id=999, cover_path="old/path.png")
        romm_api.get_rom.return_value = {"id": 42, "path_cover_large": "/c.png"}
        romm_api.download_cover.side_effect = Exception("disk full")

        result = await artwork_service.refresh_cover(42)
        assert result["success"] is False
        assert result["reason"] == "download_failed"
        assert "disk full" in result["message"]
        with uow:
            assert uow.roms.get(42).cover_path == "old/path.png"


# ── TestIsStagingFileOrphaned ─────────────────────────────────────────────────


class TestIsStagingFileOrphaned:
    """Tests for is_staging_file_orphaned()."""

    def test_orphaned_when_not_in_registry(self, artwork_service, tmp_path):
        result = artwork_service.is_staging_file_orphaned(str(tmp_path), {}, "42")
        assert result is True

    def test_orphaned_when_final_exists(self, artwork_service, file_store, tmp_path):
        final = os.path.join(str(tmp_path), "1001p.png")
        file_store.files[final] = b"final"
        registry = {"42": 1001}
        result = artwork_service.is_staging_file_orphaned(str(tmp_path), registry, "42")
        assert result is True

    def test_not_orphaned_when_no_final(self, artwork_service, tmp_path):
        registry = {"42": 1001}
        result = artwork_service.is_staging_file_orphaned(str(tmp_path), registry, "42")
        assert result is False

    def test_not_orphaned_when_no_app_id(self, artwork_service, tmp_path):
        registry = {"42": None}
        result = artwork_service.is_staging_file_orphaned(str(tmp_path), registry, "42")
        assert result is False


# ── TestPruneOrphanedStagingArtwork ──────────────────────────────────────────


class TestPruneOrphanedStagingArtwork:
    """Tests for prune_orphaned_staging_artwork()."""

    def test_removes_staging_not_in_registry(self, artwork_service, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        staging = os.path.join(grid_dir, "romm_42_cover.png")
        file_store.files[staging] = b"fake"

        steam_config.grid_dir.return_value = grid_dir

        artwork_service.prune_orphaned_staging_artwork()
        assert staging not in file_store.files

    def test_removes_redundant_staging_with_final(self, artwork_service, uow, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        staging = os.path.join(grid_dir, "romm_42_cover.png")
        final = os.path.join(grid_dir, "1001p.png")
        file_store.files[staging] = b"fake staging"
        file_store.files[final] = b"fake final"

        steam_config.grid_dir.return_value = grid_dir
        _seed_rom(uow, 42, app_id=1001, name="Game A")

        artwork_service.prune_orphaned_staging_artwork()
        assert staging not in file_store.files
        assert final in file_store.files

    def test_keeps_staging_when_no_final(self, artwork_service, uow, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        staging = os.path.join(grid_dir, "romm_42_cover.png")
        file_store.files[staging] = b"fake staging"

        steam_config.grid_dir.return_value = grid_dir
        _seed_rom(uow, 42, app_id=1001, name="Game A")

        artwork_service.prune_orphaned_staging_artwork()
        assert staging in file_store.files

    def test_ignores_non_staging_files(self, artwork_service, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        final = os.path.join(grid_dir, "1001p.png")
        other = os.path.join(grid_dir, "something_else.png")
        file_store.files[final] = b"final art"
        file_store.files[other] = b"other"

        steam_config.grid_dir.return_value = grid_dir

        artwork_service.prune_orphaned_staging_artwork()
        assert final in file_store.files
        assert other in file_store.files

    def test_no_grid_dir_no_crash(self, artwork_service, steam_config):
        steam_config.grid_dir.return_value = None
        artwork_service.prune_orphaned_staging_artwork()  # should not raise

    def test_grid_not_a_directory_no_crash(self, artwork_service, steam_config, file_store, tmp_path):
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        # No files under grid_dir => isdir returns False
        file_store.isdir_paths = set()
        artwork_service.prune_orphaned_staging_artwork()  # should not raise

    def test_handles_os_error(self, artwork_service, steam_config, file_store, tmp_path, caplog):
        grid_dir = str(tmp_path / "grid")
        staging = os.path.join(grid_dir, "romm_42_cover.png")
        file_store.files[staging] = b"fake"

        steam_config.grid_dir.return_value = grid_dir

        def boom(_path: str) -> None:
            raise OSError("permission denied")

        file_store.remove_file = boom  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            artwork_service.prune_orphaned_staging_artwork()

        assert staging in file_store.files
        assert any("Failed to remove orphaned staging artwork" in r.message for r in caplog.records)


# ── TestPruneOrphanedCoverCache ──────────────────────────────────────────────


class TestPruneOrphanedCoverCache:
    """Tests for prune_orphaned_cover_cache()."""

    def test_removes_cache_for_rom_absent_from_roms(self, artwork_service, file_store, cover_cache_dir):
        orphan = _cache(cover_cache_dir, 42)
        file_store.files[orphan] = b"orphan"
        file_store.made_dirs.add(cover_cache_dir)

        artwork_service.prune_orphaned_cover_cache()
        assert orphan not in file_store.files

    def test_keeps_cache_for_bound_and_unbound_rows(self, artwork_service, uow, file_store, cover_cache_dir):
        bound = _cache(cover_cache_dir, 1)
        unbound = _cache(cover_cache_dir, 2)
        orphan = _cache(cover_cache_dir, 3)
        file_store.files[bound] = b"a"
        file_store.files[unbound] = b"b"
        file_store.files[orphan] = b"c"
        file_store.made_dirs.add(cover_cache_dir)
        _seed_rom(uow, 1, app_id=1001)
        _seed_rom(uow, 2, app_id=None)  # a synced-but-unbound sibling keeps its cover

        artwork_service.prune_orphaned_cover_cache()
        assert bound in file_store.files
        assert unbound in file_store.files
        assert orphan not in file_store.files

    def test_ignores_non_png_and_non_numeric(self, artwork_service, file_store, cover_cache_dir):
        junk = os.path.join(cover_cache_dir, "readme.txt")
        weird = os.path.join(cover_cache_dir, "not-a-rom.png")
        file_store.files[junk] = b"x"
        file_store.files[weird] = b"y"
        file_store.made_dirs.add(cover_cache_dir)

        artwork_service.prune_orphaned_cover_cache()
        assert junk in file_store.files
        assert weird in file_store.files

    def test_sweeps_leftover_tmp_sidecars(self, artwork_service, uow, file_store, cover_cache_dir):
        """A ``.tmp`` sidecar from a crashed atomic write is swept regardless of rom_id."""
        live = _cache(cover_cache_dir, 1)
        stale_tmp = _tmp(_cache(cover_cache_dir, 1))  # a leftover sidecar for a live rom
        file_store.files[live] = b"live"
        file_store.files[stale_tmp] = b"half-written"
        file_store.made_dirs.add(cover_cache_dir)
        _seed_rom(uow, 1, app_id=1001)

        artwork_service.prune_orphaned_cover_cache()
        assert live in file_store.files  # the live cover survives
        assert stale_tmp not in file_store.files  # the sidecar is swept

    def test_no_cache_dir_no_crash(self, artwork_service, file_store):
        # is_dir is False for a dir with no files and no make_dirs record.
        artwork_service.prune_orphaned_cover_cache()  # should not raise

    def test_handles_os_error(self, artwork_service, file_store, cover_cache_dir, caplog):
        orphan = _cache(cover_cache_dir, 42)
        file_store.files[orphan] = b"orphan"
        file_store.made_dirs.add(cover_cache_dir)

        def boom(_path: str) -> None:
            raise OSError("permission denied")

        file_store.remove_file = boom  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            artwork_service.prune_orphaned_cover_cache()

        assert orphan in file_store.files
        assert any("Failed to remove orphaned cover cache" in r.message for r in caplog.records)


# ── TestAtomicWrites ─────────────────────────────────────────────────────────


class TestAtomicWrites:
    """Cover writes land in a ``.tmp`` sidecar then atomic-rename over the target."""

    @pytest.mark.asyncio
    async def test_download_failed_rename_leaves_no_sidecar(
        self, artwork_service, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """If the post-download rename fails, the ``.tmp`` sidecar is cleaned up."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        romm_api.download_cover.side_effect = _writing_download(file_store)
        file_store.rename_failures.add(_tmp(cache))  # rename tmp→cache blows up

        roms = [{"id": 42, "name": "G", "path_cover_large": "/c.png"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        # The failed write yields no result entry, no cache file, and no sidecar.
        assert 42 not in result
        assert cache not in file_store.files
        assert _tmp(cache) not in file_store.files

    def test_finalize_publishes_grid_via_tmp_then_rename(self, artwork_service, file_store, cover_cache_dir, tmp_path):
        """finalize copies the cache cover into a grid ``.tmp`` then renames it over
        ``{app_id}p.png`` — atomically, leaving the cache intact and no sidecar."""
        grid = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 1)
        file_store.files[cache] = b"cover data"

        result = artwork_service.finalize_cover_path(grid, cache, 100001, "1")
        final = os.path.join(grid, "100001p.png")
        assert result == cache
        assert file_store.files[cache] == b"cover data"  # cache survives
        assert file_store.files[final] == b"cover data"  # grid published
        assert _tmp(final) not in file_store.files  # no sidecar left behind
