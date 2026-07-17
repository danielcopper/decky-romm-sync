"""Tests for ArtworkService."""

import asyncio
import base64
import logging
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock

# conftest.py patches decky before this import
import decky
import pytest
from fakes.fake_cover_art_file_store import FakeCoverArtFileStore
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from models.cover import CoverRevalidation

from domain.artwork_paths import cover_meta_filename
from domain.cover_refresh import scan_cover_refresh_candidates
from domain.rom import Rom
from lib.errors import RommConnectionError, RommNotFoundError
from services.artwork import ArtworkService, ArtworkServiceConfig


def _seed_rom(
    uow,
    rom_id,
    *,
    app_id,
    cover_path=None,
    cover_source=None,
    platform_slug="n64",
    name="Game",
    sgdb_id=None,
    group_key=None,
):
    """Insert a bound (or unbound when app_id is None) ROM into the fake UoW."""
    rom = Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=name,
        fs_name=f"{name}.z64",
        shortcut_app_id=app_id,
        last_synced_at="2025-01-01T00:00:00",
        cover_path=cover_path,
        cover_source=cover_source,
        sgdb_id=sgdb_id,
        sibling_group_key=group_key,
    )
    with uow:
        uow.roms.save(rom)


def _tmp(path: str) -> str:
    """The atomic-write sidecar path (append ``.tmp``)."""
    return path + ".tmp"


def _writing_download(file_store, payload: bytes = b"downloaded", *, resp_etag=None, resp_last_modified=None):
    """A ``download_cover`` side effect that materializes its dest (the ``.tmp`` sidecar).

    The atomic download streams to ``dest.tmp`` then renames it over the cache
    file, so the mock must actually create the dest for the rename to succeed —
    mirroring the real adapter's post-download contract. Returns a 200-style
    :class:`CoverRevalidation` carrying the response validators (*resp_etag* /
    *resp_last_modified*, default none) so the service records the cover-meta
    sidecar (#1454); accepts and ignores the conditional request kwargs.
    """

    def _dl(_url, dest, *, etag=None, last_modified=None):
        file_store.files[dest] = payload
        return CoverRevalidation(not_modified=False, etag=resp_etag, last_modified=resp_last_modified)

    return _dl


def _conditional_download(file_store, *, resp: CoverRevalidation, payload: bytes = b"fresh"):
    """A ``download_cover`` side effect that returns *resp* (#1454).

    Writes *payload* to dest only on a 200 (``not_modified=False``), mirroring the
    real adapter: a 304 leaves the destination untouched so the cached bytes are
    kept. The conditional request kwargs are recorded on the mock for assertions.
    """

    def _dl(_url, dest, *, etag=None, last_modified=None):
        if not resp.not_modified:
            file_store.files[dest] = payload
        return resp

    return _dl


def _seed_meta(file_store, cover_cache_dir, rom_id, *, etag=None, last_modified=None):
    """Stage a validator sidecar ({rom_id}.cover-meta.json) for *rom_id* (#1454)."""
    import json

    path = os.path.join(cover_cache_dir, cover_meta_filename(rom_id))
    file_store.files[path] = json.dumps({"etag": etag, "last_modified": last_modified}).encode("utf-8")


def _read_meta(file_store, cover_cache_dir, rom_id) -> dict[str, Any]:
    """Return the parsed validator sidecar dict for *rom_id* (raises if absent)."""
    import json

    path = os.path.join(cover_cache_dir, cover_meta_filename(rom_id))
    return json.loads(file_store.files[path])


def _registry(uow):
    """The bound-row registry projection the orchestrator hands the refresh pass (#1386).

    Mirrors ``_read_apply_registry``'s contract slice: bound rows only, keyed by
    ``str(rom_id)``, each entry carrying ``app_id`` + ``cover_source``.
    """
    with uow:
        return {
            str(rom.rom_id): {"app_id": rom.shortcut_app_id, "cover_source": rom.cover_source}
            for rom in uow.roms.iter_all()
            if rom.shortcut_app_id is not None
        }


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

    @pytest.mark.asyncio
    async def test_changed_fingerprint_re_downloads_over_cache(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A stored cover_source differing from the fresh one blocks the cache
        reuse — the server-side cover changed, so the stale bytes are replaced
        by a fresh download (#1386)."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"stale cached"
        _seed_rom(uow, 42, app_id=99999, cover_source="/cover.png?ts=2026-01-01 00:00:00")
        romm_api.download_cover.side_effect = _writing_download(file_store, b"fresh cover")

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png?ts=2026-07-11 12:00:00"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert result[42] == cache
        romm_api.download_cover.assert_called_once()
        assert file_store.files[cache] == b"fresh cover"

    @pytest.mark.asyncio
    async def test_unchanged_fingerprint_reuses_cache(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A stored cover_source equal to the fresh one keeps the cache-hit
        short-circuit — no download."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cached"
        _seed_rom(uow, 42, app_id=99999, cover_source="/cover.png?ts=2026-01-01 00:00:00")

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png?ts=2026-01-01 00:00:00"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert result[42] == cache
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_null_fingerprint_adopts_existing_cache(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A NULL stored cover_source (pre-#1386 row) with an existing cache file
        adopts the local bytes — no re-download of the whole library on upgrade."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cached"
        _seed_rom(uow, 42, app_id=99999, cover_source=None)

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png?ts=2026-07-11 12:00:00"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert result[42] == cache
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_changed_fingerprint_blocks_grid_seed(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A changed fingerprint must not seed the cache from the grid copy either
        — the grid file predates the change, so a fresh download wins."""
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        final = os.path.join(grid_dir, "99999p.png")
        file_store.files[final] = b"stale grid cover"
        _seed_rom(uow, 42, app_id=99999, cover_source="/cover.png?ts=2026-01-01 00:00:00")
        romm_api.download_cover.side_effect = _writing_download(file_store, b"fresh cover")

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png?ts=2026-07-11 12:00:00"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        cache = _cache(cover_cache_dir, 42)
        assert result[42] == cache
        romm_api.download_cover.assert_called_once()
        assert file_store.files[cache] == b"fresh cover"

    @pytest.mark.asyncio
    async def test_changed_fingerprint_download_failure_keeps_old_cache(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A failed re-download leaves the old cache bytes intact (atomic
        tmp+rename) — never a broken/missing cover."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"old cached"
        _seed_rom(uow, 42, app_id=99999, cover_source="/cover.png?ts=2026-01-01 00:00:00")
        romm_api.download_cover.side_effect = Exception("network down")

        roms = [{"id": 42, "name": "Test Game", "path_cover_large": "/cover.png?ts=2026-07-11 12:00:00"}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )

        assert 42 not in result
        assert file_store.files[cache] == b"old cached"
        assert _tmp(cache) not in file_store.files


# ── TestUrlCoverFallback ──────────────────────────────────────────────────────


class TestUrlCoverFallback:
    """The url_cover fallback (#1450): a definitive 404 on the RomM cover asset
    retries once against the ROM's external ``url_cover``.

    The fallback bytes come from ``download_cover_from_url`` — the bearer-free
    adapter path (the no-bearer guarantee itself is pinned on the HTTP adapter,
    ``tests/adapters/romm/test_http.py::TestDownloadExternal``). Here we pin the
    routing (fallback fires only on 404, records the applied source truthfully).
    """

    _URL_COVER = "https://cdn.example.com/grid/abc.png"

    @pytest.mark.asyncio
    async def test_404_falls_back_to_url_cover_and_records_applied_source(
        self, artwork_service, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        romm_api.download_cover.side_effect = RommNotFoundError("HTTP 404: Not Found")
        romm_api.download_cover_from_url.side_effect = _writing_download(file_store, b"cdn art")

        applied: dict[int, str] = {}
        roms = [{"id": 42, "name": "Game", "path_cover_large": "/cover.png", "url_cover": self._URL_COVER}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling, applied_sources=applied
        )

        cache = _cache(cover_cache_dir, 42)
        assert result[42] == cache
        assert file_store.files[cache] == b"cdn art"
        assert _tmp(cache) not in file_store.files
        # The external url_cover was fetched into the .tmp sidecar (not the RomM path).
        romm_api.download_cover_from_url.assert_called_once()
        assert romm_api.download_cover_from_url.call_args[0] == (self._URL_COVER, _tmp(cache))
        # The applied source is the url_cover, not the 404'd path_cover.
        assert applied[42] == self._URL_COVER

    @pytest.mark.asyncio
    async def test_404_without_url_cover_keeps_todays_failure(self, artwork_service, steam_config, romm_api, tmp_path):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        romm_api.download_cover.side_effect = RommNotFoundError("HTTP 404: Not Found")

        roms = [{"id": 42, "name": "Game", "path_cover_large": "/cover.png"}]  # no url_cover
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert 42 not in result
        romm_api.download_cover_from_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_404_with_empty_url_cover_keeps_todays_failure(
        self, artwork_service, steam_config, romm_api, tmp_path
    ):
        """An empty ``url_cover`` string is falsy — no fallback, today's path."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        romm_api.download_cover.side_effect = RommNotFoundError("HTTP 404: Not Found")

        roms = [{"id": 42, "name": "Game", "path_cover_large": "/cover.png", "url_cover": ""}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert 42 not in result
        romm_api.download_cover_from_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_404_and_url_cover_also_failing_gives_up_once(
        self, artwork_service, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        romm_api.download_cover.side_effect = RommNotFoundError("HTTP 404: Not Found")
        romm_api.download_cover_from_url.side_effect = RommNotFoundError("HTTP 404: cdn miss")

        roms = [{"id": 42, "name": "Game", "path_cover_large": "/cover.png", "url_cover": self._URL_COVER}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert 42 not in result
        # Exactly one fallback attempt — the ROM is not retried in a loop.
        romm_api.download_cover_from_url.assert_called_once()
        # The broken write leaves no sidecar behind.
        assert _tmp(_cache(cover_cache_dir, 42)) not in file_store.files

    @pytest.mark.asyncio
    async def test_transport_error_does_not_fall_back(self, artwork_service, steam_config, romm_api, tmp_path):
        """A transient transport error keeps today's retry-ladder behaviour — the
        url_cover fallback fires ONLY on a definitive 404."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        romm_api.download_cover.side_effect = RommConnectionError("connection refused")

        roms = [{"id": 42, "name": "Game", "path_cover_large": "/cover.png", "url_cover": self._URL_COVER}]
        result = await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling
        )
        assert 42 not in result
        romm_api.download_cover_from_url.assert_not_called()

    @pytest.mark.asyncio
    async def test_applied_url_cover_lets_refresh_detect_a_later_change(
        self, artwork_service, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """Recording url_cover as the applied source keeps the refresh compare
        truthful: a later fetch whose fresh source is the (now-fixed) RomM
        path_cover is detected as a change instead of being silently skipped."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        romm_api.download_cover.side_effect = RommNotFoundError("HTTP 404: Not Found")
        romm_api.download_cover_from_url.side_effect = _writing_download(file_store, b"cdn art")

        applied: dict[int, str] = {}
        path_cover = "/cover.png?ts=2026-07-11 12:00:00"
        roms = [{"id": 42, "name": "Game", "path_cover_large": path_cover, "url_cover": self._URL_COVER}]
        await artwork_service.download_artwork(
            roms, emit_progress=_noop_emit_progress, is_cancelling=_not_cancelling, applied_sources=applied
        )

        # The row's cover_source would be the applied url_cover.
        registry = {"42": {"app_id": 99999, "cover_source": applied[42]}}
        # A later sync fetches the (fixed) RomM asset — the compare flags it changed.
        scan = scan_cover_refresh_candidates([{"id": 42, "path_cover_large": path_cover}], registry)
        assert scan.changed == [(42, 99999, path_cover)]


# ── TestRefreshChangedCovers ──────────────────────────────────────────────────


class TestRefreshChangedCovers:
    """The #1386 cover-cache invalidation pass over a unit's fetched ROMs."""

    _OLD = "/cover/big.png?ts=2026-01-01 00:00:00"
    _NEW = "/cover/big.png?ts=2026-07-11 12:00:00"

    @pytest.mark.asyncio
    async def test_changed_bound_rom_re_downloads_publishes_and_persists(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"stale"
        _seed_rom(uow, 42, app_id=99999, cover_source=self._OLD)
        romm_api.download_cover.side_effect = _writing_download(file_store, b"fresh cover")

        refreshed = await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        assert refreshed == [{"rom_id": 42, "app_id": 99999}]
        # Downloaded via the tmp sidecar into the cache; grid copy republished.
        romm_api.download_cover.assert_called_once()
        assert romm_api.download_cover.call_args[0] == (self._NEW, _tmp(cache))
        assert file_store.files[cache] == b"fresh cover"
        assert file_store.files[os.path.join(grid_dir, "99999p.png")] == b"fresh cover"
        # The fresh fingerprint is persisted — the exact opaque string.
        with uow:
            assert uow.roms.get(42).cover_source == self._NEW

    @pytest.mark.asyncio
    async def test_404_falls_back_to_url_cover_and_persists_applied_source(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A 404 on the changed RomM cover retries against ``url_cover`` and
        persists the url_cover as the fingerprint (the source actually applied,
        #1450) — not the 404'd RomM path."""
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"stale"
        _seed_rom(uow, 42, app_id=99999, cover_source=self._OLD)
        url_cover = "https://cdn.example.com/grid/abc.png"
        romm_api.download_cover.side_effect = RommNotFoundError("HTTP 404: Not Found")
        romm_api.download_cover_from_url.side_effect = _writing_download(file_store, b"cdn art")

        refreshed = await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW, "url_cover": url_cover}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        assert refreshed == [{"rom_id": 42, "app_id": 99999}]
        romm_api.download_cover_from_url.assert_called_once()
        assert file_store.files[cache] == b"cdn art"
        assert file_store.files[os.path.join(grid_dir, "99999p.png")] == b"cdn art"
        with uow:
            assert uow.roms.get(42).cover_source == url_cover

    @pytest.mark.asyncio
    async def test_null_fingerprint_with_cache_adopts_without_download(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """The NULL-adopt: a pre-#1386 row with a cache file persists the fresh
        fingerprint WITHOUT re-downloading — no thundering herd on upgrade."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cached"
        _seed_rom(uow, 42, app_id=99999, cover_source=None)

        refreshed = await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        assert refreshed == []
        romm_api.download_cover.assert_not_called()
        assert file_store.files[cache] == b"cached"
        with uow:
            assert uow.roms.get(42).cover_source == self._NEW

    @pytest.mark.asyncio
    async def test_null_fingerprint_without_cache_left_for_apply_path(
        self, artwork_service, uow, steam_config, romm_api, tmp_path
    ):
        """NULL fingerprint + no cache file = today's behaviour: nothing here,
        the cover downloads when the ROM rides the apply path."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        _seed_rom(uow, 42, app_id=99999, cover_source=None)

        refreshed = await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        assert refreshed == []
        romm_api.download_cover.assert_not_called()
        with uow:
            assert uow.roms.get(42).cover_source is None

    @pytest.mark.asyncio
    async def test_unchanged_fingerprint_is_a_noop(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        file_store.files[_cache(cover_cache_dir, 42)] = b"cached"
        _seed_rom(uow, 42, app_id=99999, cover_source=self._NEW)

        refreshed = await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        assert refreshed == []
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_unbound_rom_is_ignored(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """An unbound sibling has no Steam tile to refresh — the apply path owns
        its cover; the pass never touches it."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        file_store.files[_cache(cover_cache_dir, 42)] = b"stale"
        _seed_rom(uow, 42, app_id=None, cover_source=self._OLD)

        refreshed = await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        assert refreshed == []
        romm_api.download_cover.assert_not_called()
        with uow:
            assert uow.roms.get(42).cover_source == self._OLD

    @pytest.mark.asyncio
    async def test_rom_without_row_or_cover_url_is_skipped(
        self, artwork_service, uow, steam_config, romm_api, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        _seed_rom(uow, 7, app_id=1000, cover_source=self._OLD)

        refreshed = await artwork_service.refresh_changed_covers(
            [
                {"id": 999, "name": "No Row", "path_cover_large": self._NEW},
                {"id": 7, "name": "No Cover"},
            ],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        assert refreshed == []
        romm_api.download_cover.assert_not_called()

    @pytest.mark.asyncio
    async def test_download_failure_keeps_old_cache_and_fingerprint_and_continues(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """One ROM's failed download leaves its old cache + fingerprint intact
        (the change is retried next sync) and never aborts the rest of the pass."""
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        cache_1 = _cache(cover_cache_dir, 1)
        cache_2 = _cache(cover_cache_dir, 2)
        file_store.files[cache_1] = b"old one"
        file_store.files[cache_2] = b"old two"
        _seed_rom(uow, 1, app_id=1001, name="One", cover_source=self._OLD)
        _seed_rom(uow, 2, app_id=1002, name="Two", cover_source=self._OLD)

        def fail_first(url, dest, *, etag=None, last_modified=None):
            if "/1.png" in url:
                raise Exception("network blip")
            file_store.files[dest] = b"fresh two"
            return CoverRevalidation(not_modified=False, etag=None, last_modified=None)

        romm_api.download_cover.side_effect = fail_first

        refreshed = await artwork_service.refresh_changed_covers(
            [
                {"id": 1, "name": "One", "path_cover_large": "/1.png?ts=2026-07-11 12:00:00"},
                {"id": 2, "name": "Two", "path_cover_large": "/2.png?ts=2026-07-11 12:00:00"},
            ],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        # ROM 1 failed: old cache bytes and old fingerprint survive; no entry.
        assert file_store.files[cache_1] == b"old one"
        assert _tmp(cache_1) not in file_store.files
        with uow:
            assert uow.roms.get(1).cover_source == self._OLD
            assert uow.roms.get(2).cover_source == "/2.png?ts=2026-07-11 12:00:00"
        # ROM 2 still refreshed.
        assert refreshed == [{"rom_id": 2, "app_id": 1002}]
        assert file_store.files[cache_2] == b"fresh two"

    @pytest.mark.asyncio
    async def test_missing_grid_dir_still_refreshes_cache_and_fingerprint(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir
    ):
        """No grid dir: the cache (the source of truth) is refreshed and the
        fingerprint persisted; only the grid publish is skipped."""
        steam_config.grid_dir.return_value = None
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"stale"
        _seed_rom(uow, 42, app_id=99999, cover_source=self._OLD)
        romm_api.download_cover.side_effect = _writing_download(file_store, b"fresh cover")

        refreshed = await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        assert refreshed == [{"rom_id": 42, "app_id": 99999}]
        assert file_store.files[cache] == b"fresh cover"
        with uow:
            assert uow.roms.get(42).cover_source == self._NEW

    @pytest.mark.asyncio
    async def test_cancel_stops_the_download_loop(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A cancel observed between downloads stops the pass; already-refreshed
        entries are still returned."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        _seed_rom(uow, 1, app_id=1001, name="One", cover_source=self._OLD)
        _seed_rom(uow, 2, app_id=1002, name="Two", cover_source=self._OLD)
        romm_api.download_cover.side_effect = _writing_download(file_store, b"fresh")
        cancelled = False

        def cancel_after_first():
            nonlocal cancelled
            was = cancelled
            cancelled = True
            return was

        refreshed = await artwork_service.refresh_changed_covers(
            [
                {"id": 1, "name": "One", "path_cover_large": "/1.png?ts=x"},
                {"id": 2, "name": "Two", "path_cover_large": "/2.png?ts=x"},
            ],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=cancel_after_first,
        )

        assert refreshed == [{"rom_id": 1, "app_id": 1001}]
        romm_api.download_cover.assert_called_once()

    @pytest.mark.asyncio
    async def test_progress_frames_are_labelled(
        self, artwork_service, uow, steam_config, file_store, romm_api, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        _seed_rom(uow, 1, app_id=1001, name="One", cover_source=self._OLD)
        romm_api.download_cover.side_effect = _writing_download(file_store, b"fresh")
        frames: list[dict[str, Any]] = []

        async def record(stage, **kwargs):
            frames.append({"stage": stage, **kwargs})

        await artwork_service.refresh_changed_covers(
            [{"id": 1, "name": "One", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=record,
            is_cancelling=_not_cancelling,
            progress_step=2,
            progress_total_steps=5,
            label="N64",
        )

        assert frames, "a changed cover must narrate progress"
        assert frames[0]["message"] == "Refreshing covers for N64 (1/1)"
        assert frames[0]["step"] == 2
        assert frames[0]["total_steps"] == 5
        # The refresh pass shares the ``covers`` sub-slice with the download loop (#1407).
        assert frames[0]["sub_stage"] == "covers"


# ── TestCoverRevalidation ─────────────────────────────────────────────────────


class TestCoverRevalidation:
    """The #1454 conditional-request revalidation decision matrix.

    A ts-only fingerprint change with a stored validator revalidates (304 keeps
    the bytes, 200 replaces them) instead of re-downloading; anything else — no
    validator, a non-ts path change — plain-downloads and (re)seeds the sidecar.
    Driven mostly through ``refresh_changed_covers`` (the invalidation pass a
    rescan actually hits) so the DB fingerprint persist is asserted directly.
    """

    _OLD = "/cover/big.png?ts=2026-01-01 00:00:00"
    _NEW = "/cover/big.png?ts=2026-07-11 12:00:00"

    @pytest.mark.asyncio
    async def test_ts_only_change_304_keeps_bytes_and_adopts_fingerprint(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        grid_dir = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid_dir
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cached bytes"
        _seed_meta(file_store, cover_cache_dir, 42, etag='"v1"')
        _seed_rom(uow, 42, app_id=99999, cover_source=self._OLD)
        romm_api.download_cover.side_effect = _conditional_download(
            file_store, resp=CoverRevalidation(not_modified=True, etag='"v1"', last_modified=None)
        )

        refreshed = await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        # The conditional request carried the stored validator.
        assert romm_api.download_cover.call_args.kwargs["etag"] == '"v1"'
        # A 304 kept the cached bytes (no re-download over them).
        assert file_store.files[cache] == b"cached bytes"
        # The fresh fingerprint was adopted so the NEXT sync is clean.
        with uow:
            assert uow.roms.get(42).cover_source == self._NEW
        # No tile re-apply and no grid churn — the tile was already current.
        assert refreshed == []
        assert os.path.join(grid_dir, "99999p.png") not in file_store.files

    @pytest.mark.asyncio
    async def test_ts_only_change_200_replaces_bytes_and_validator(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"stale bytes"
        _seed_meta(file_store, cover_cache_dir, 42, etag='"v1"')
        _seed_rom(uow, 42, app_id=99999, cover_source=self._OLD)
        romm_api.download_cover.side_effect = _conditional_download(
            file_store,
            resp=CoverRevalidation(not_modified=False, etag='"v2"', last_modified=None),
            payload=b"new bytes",
        )

        await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        # A genuine 200 replaced the cache bytes and refreshed the validator.
        assert romm_api.download_cover.call_args.kwargs["etag"] == '"v1"'
        assert file_store.files[cache] == b"new bytes"
        assert _read_meta(file_store, cover_cache_dir, 42)["etag"] == '"v2"'
        with uow:
            assert uow.roms.get(42).cover_source == self._NEW

    @pytest.mark.asyncio
    async def test_no_validator_plain_downloads_and_seeds_one(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"stale bytes"
        # No sidecar seeded — a ts change can't revalidate.
        _seed_rom(uow, 42, app_id=99999, cover_source=self._OLD)
        romm_api.download_cover.side_effect = _conditional_download(
            file_store, resp=CoverRevalidation(not_modified=False, etag='"v9"', last_modified=None), payload=b"fresh"
        )

        await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        # No stored validator → no conditional header, a plain download…
        assert romm_api.download_cover.call_args.kwargs["etag"] is None
        assert file_store.files[cache] == b"fresh"
        # …and the response validator is SEEDED for the next sync to revalidate.
        assert _read_meta(file_store, cover_cache_dir, 42)["etag"] == '"v9"'

    @pytest.mark.asyncio
    async def test_non_ts_path_change_plain_downloads(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"old bytes"
        _seed_meta(file_store, cover_cache_dir, 42, etag='"v1"')
        _seed_rom(uow, 42, app_id=99999, cover_source="/cover/old.png?ts=1")
        romm_api.download_cover.side_effect = _conditional_download(
            file_store, resp=CoverRevalidation(not_modified=False, etag='"v2"', last_modified=None), payload=b"fresh"
        )

        # The path itself changed (old.png → new.png), not just the ts — even with
        # a validator present this must NOT revalidate.
        await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": "/cover/new.png?ts=2"}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        assert romm_api.download_cover.call_args.kwargs["etag"] is None
        assert file_store.files[cache] == b"fresh"

    @pytest.mark.asyncio
    async def test_revalidation_transport_error_keeps_old_bytes_and_fingerprint(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """A conditional-request transport error is today's failed-download path."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"old bytes"
        _seed_meta(file_store, cover_cache_dir, 42, etag='"v1"')
        _seed_rom(uow, 42, app_id=99999, cover_source=self._OLD)
        romm_api.download_cover.side_effect = RommConnectionError("connection refused")

        refreshed = await artwork_service.refresh_changed_covers(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            _registry(uow),
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
        )

        # Nothing advanced: old bytes, old fingerprint, no refresh entry, no sidecar churn.
        assert file_store.files[cache] == b"old bytes"
        assert _tmp(cache) not in file_store.files
        with uow:
            assert uow.roms.get(42).cover_source == self._OLD
        assert refreshed == []

    @pytest.mark.asyncio
    async def test_apply_path_304_adopts_fresh_source_via_accumulator(
        self, artwork_service, uow, steam_config, file_store, romm_api, cover_cache_dir, tmp_path
    ):
        """The download_artwork apply path revalidates too, threading the fresh
        source through ``applied_sources`` (the reporter persists it as cover_source)."""
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cached bytes"
        _seed_meta(file_store, cover_cache_dir, 42, etag='"v1"')
        _seed_rom(uow, 42, app_id=99999, cover_source=self._OLD)
        romm_api.download_cover.side_effect = _conditional_download(
            file_store, resp=CoverRevalidation(not_modified=True, etag='"v1"', last_modified=None)
        )

        applied: dict[int, str] = {}
        result = await artwork_service.download_artwork(
            [{"id": 42, "name": "Game", "path_cover_large": self._NEW}],
            emit_progress=_noop_emit_progress,
            is_cancelling=_not_cancelling,
            applied_sources=applied,
        )

        assert result[42] == cache
        assert file_store.files[cache] == b"cached bytes"  # 304 kept the bytes
        assert applied[42] == self._NEW  # fresh fingerprint flows to the reporter


# ── TestDownloadArtworkProgress ───────────────────────────────────────────────


class TestDownloadArtworkProgress:
    """Cover-download progress frames — throttled, ``fetching`` stage, labelled (#1025).

    The cover phase runs in the per-unit prep, so it narrates under the same
    ``fetching`` stage as the paginated fetch and is throttled (first + last +
    every Nth cover) so a ~1300-cover library does not flood the WebSocket
    bridge with a frame per cover.
    """

    @pytest.mark.asyncio
    async def test_throttles_and_labels_cover_frames(
        self, artwork_service, steam_config, file_store, romm_api, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        romm_api.download_cover.side_effect = _writing_download(file_store)
        frames: list[dict[str, Any]] = []

        async def record(stage, **kwargs):
            frames.append({"stage": str(stage), **kwargs})

        # 120 covers → first + every 50th + last = covers 1, 50, 100, 120.
        roms = [{"id": i, "name": f"G{i}", "path_cover_large": f"/c{i}.png"} for i in range(120)]
        await artwork_service.download_artwork(
            roms,
            emit_progress=record,
            is_cancelling=_not_cancelling,
            progress_step=3,
            progress_total_steps=9,
            label="Game Boy Advance",
        )

        assert [f["current"] for f in frames] == [1, 50, 100, 120]
        for f in frames:
            assert f["stage"] == "fetching"
            # The cover phase carries the ``covers`` sub-stage so the bar fills the
            # unit's covers sub-slice, above the fetch share (#1407).
            assert f["sub_stage"] == "covers"
            assert f["total"] == 120
            assert f["step"] == 3
            assert f["total_steps"] == 9
        assert frames[0]["message"] == "Preparing covers for Game Boy Advance (1/120)"
        assert frames[-1]["message"] == "Preparing covers for Game Boy Advance (120/120)"

    @pytest.mark.asyncio
    async def test_blank_label_falls_back_to_bare_message(
        self, artwork_service, steam_config, file_store, romm_api, tmp_path
    ):
        steam_config.grid_dir.return_value = str(tmp_path / "grid")
        romm_api.download_cover.side_effect = _writing_download(file_store)
        frames: list[dict[str, Any]] = []

        async def record(stage, **kwargs):
            frames.append({"stage": str(stage), **kwargs})

        roms = [{"id": 1, "name": "Solo", "path_cover_large": "/c.png"}]
        await artwork_service.download_artwork(roms, emit_progress=record, is_cancelling=_not_cancelling)

        assert frames[0]["message"] == "Preparing covers (1/1)"


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

    def test_removes_validator_sidecar(self, artwork_service, file_store, cover_cache_dir, tmp_path):
        """The #1454 cover-meta sidecar is swept with the removed shortcut's cache."""
        grid = str(tmp_path)
        cache = _cache(cover_cache_dir, 42)
        file_store.files[cache] = b"cache cover"
        _seed_meta(file_store, cover_cache_dir, 42, etag='"v1"')
        meta = os.path.join(cover_cache_dir, cover_meta_filename(42))
        entry = {"cover_path": "", "app_id": 100001}
        artwork_service.remove_artwork_files(grid, 42, entry)
        assert cache not in file_store.files
        assert meta not in file_store.files

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

    def test_sweeps_all_grid_forms_for_app_id(self, artwork_service, file_store, tmp_path):
        """Every suffix-by-extension form for the removed appId is deleted."""
        grid = str(tmp_path)
        staged = [
            os.path.join(grid, f"2200000001{suffix}.{ext}")
            for suffix in ("p", "_hero", "_logo", "_icon", "")
            for ext in ("png", "jpg", "jpeg")
        ]
        for path in staged:
            file_store.files[path] = b"art"
        entry = {"cover_path": "", "app_id": 2200000001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        for path in staged:
            assert path not in file_store.files

    def test_sweep_leaves_other_app_ids_untouched(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        other_portrait = os.path.join(grid, "2200000002p.png")
        other_hero = os.path.join(grid, "2200000002_hero.png")
        file_store.files[other_portrait] = b"other"
        file_store.files[other_hero] = b"other"
        entry = {"cover_path": "", "app_id": 2200000001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert other_portrait in file_store.files
        assert other_hero in file_store.files

    def test_removes_grid_cover_even_when_cover_path_removed(
        self, artwork_service, file_store, cover_cache_dir, tmp_path
    ):
        """A removed cover_path (the cache file) no longer short-circuits the grid sweep."""
        grid = str(tmp_path)
        cache = _cache(cover_cache_dir, 42)
        portrait = os.path.join(grid, "2200000001p.png")
        hero = os.path.join(grid, "2200000001_hero.png")
        file_store.files[cache] = b"cache cover"
        file_store.files[portrait] = b"grid cover"
        file_store.files[hero] = b"hero"
        entry = {"cover_path": cache, "app_id": 2200000001}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert cache not in file_store.files
        assert portrait not in file_store.files
        assert hero not in file_store.files

    def test_sweeps_legacy_artwork_id_forms(self, artwork_service, file_store, tmp_path):
        grid = str(tmp_path)
        portrait = os.path.join(grid, "12345p.png")
        wide = os.path.join(grid, "12345.jpg")
        file_store.files[portrait] = b"data"
        file_store.files[wide] = b"data"
        entry = {"cover_path": "", "artwork_id": 12345}
        artwork_service.remove_artwork_files(grid, "42", entry)
        assert portrait not in file_store.files
        assert wide not in file_store.files


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
        romm_api.download_cover.side_effect = _writing_download(file_store, b"downloaded cover")

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
        romm_api.download_cover.side_effect = _writing_download(file_store, b"server-only cover")

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
    async def test_404_falls_back_to_url_cover(self, artwork_service, romm_api, file_store, cover_cache_dir):
        romm_api.get_rom.return_value = {
            "id": 42,
            "path_cover_large": "/c.png",
            "url_cover": "https://cdn.example.com/x.png",
        }
        romm_api.download_cover.side_effect = RommNotFoundError("HTTP 404: Not Found")
        romm_api.download_cover_from_url.side_effect = _writing_download(file_store, b"cdn cover")

        result = await artwork_service.fetch_cover_base64(42)
        assert base64.b64decode(result["base64"]) == b"cdn cover"
        romm_api.download_cover_from_url.assert_called_once()
        assert file_store.files[_cache(cover_cache_dir, 42)] == b"cdn cover"

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
        romm_api.download_cover.side_effect = _writing_download(file_store, b"new cover bytes")

        result = await artwork_service.refresh_cover(42)

        cache = _cache(cover_cache_dir, 42)
        expected_final = os.path.join(grid, "999p.png")
        # The persisted cover_path is the CACHE path; the grid gets a copy. The
        # repair also stamps the confirmed cover fingerprint (#1386).
        assert result == {"success": True, "message": "Cover refreshed", "cover_path": cache}
        with uow:
            assert uow.roms.get(42).cover_path == cache
            assert uow.roms.get(42).cover_source == "/c.png"
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
        romm_api.download_cover.side_effect = _writing_download(file_store, b"small")

        result = await artwork_service.refresh_cover(42)
        assert result["success"] is True
        romm_api.download_cover.assert_called_once()
        assert romm_api.download_cover.call_args[0][0] == "/small.png"
        with uow:
            assert uow.roms.get(42).cover_path == _cache(cover_cache_dir, 42)

    @pytest.mark.asyncio
    async def test_404_falls_back_to_url_cover_and_records_it(
        self,
        artwork_service,
        uow,
        steam_config,
        file_store,
        romm_api,
        cover_cache_dir,
        tmp_path,
    ):
        """A 404 on the RomM cover retries against ``url_cover`` and records the
        url_cover as the confirmed fingerprint (the applied source, #1450)."""
        grid = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid
        _seed_rom(uow, 42, app_id=999, cover_path="")
        url_cover = "https://cdn.example.com/x.png"
        romm_api.get_rom.return_value = {"id": 42, "path_cover_large": "/c.png", "url_cover": url_cover}
        romm_api.download_cover.side_effect = RommNotFoundError("HTTP 404: Not Found")
        romm_api.download_cover_from_url.side_effect = _writing_download(file_store, b"cdn")

        result = await artwork_service.refresh_cover(42)

        cache = _cache(cover_cache_dir, 42)
        assert result == {"success": True, "message": "Cover refreshed", "cover_path": cache}
        romm_api.download_cover_from_url.assert_called_once()
        assert file_store.files[cache] == b"cdn"
        with uow:
            assert uow.roms.get(42).cover_path == cache
            assert uow.roms.get(42).cover_source == url_cover

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

    def test_sweeps_orphaned_validator_sidecar_but_keeps_live_one(
        self, artwork_service, uow, file_store, cover_cache_dir
    ):
        """A validator sidecar (#1454) is pruned when its rom_id has no row, kept when live."""
        live_meta = os.path.join(cover_cache_dir, cover_meta_filename(1))
        orphan_meta = os.path.join(cover_cache_dir, cover_meta_filename(3))
        _seed_meta(file_store, cover_cache_dir, 1, etag='"live"')
        _seed_meta(file_store, cover_cache_dir, 3, etag='"orphan"')
        file_store.files[_cache(cover_cache_dir, 1)] = b"live"
        file_store.made_dirs.add(cover_cache_dir)
        _seed_rom(uow, 1, app_id=1001)

        artwork_service.prune_orphaned_cover_cache()
        assert live_meta in file_store.files
        assert orphan_meta not in file_store.files

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


# ── TestCleanupOrphanedGridImages ────────────────────────────────────────────


class TestCleanupOrphanedGridImages:
    """Tests for cleanup_orphaned_grid_images() — the user-triggered orphan delete."""

    # In-range appIds (high bit set): orphan, live-foreign, bound.
    ORPHAN = 2200000001
    FOREIGN = 2200000002
    BOUND = 2200000003

    def _grid(self, steam_config, tmp_path) -> str:
        grid = str(tmp_path / "grid")
        steam_config.grid_dir.return_value = grid
        return grid

    @pytest.mark.asyncio
    async def test_orphan_in_range_deleted(self, artwork_service, steam_config, file_store, tmp_path):
        grid = self._grid(steam_config, tmp_path)
        orphan = os.path.join(grid, f"{self.ORPHAN}p.png")
        file_store.files[orphan] = b"orphan"

        result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=False)
        assert result == {"success": True, "removed_count": 1}
        assert orphan not in file_store.files

    @pytest.mark.asyncio
    async def test_store_game_art_never_a_candidate(self, artwork_service, steam_config, file_store, tmp_path):
        """User custom art for a regular Steam game (small appId) survives any live set."""
        grid = self._grid(steam_config, tmp_path)
        store_art = os.path.join(grid, "570p.png")
        file_store.files[store_art] = b"store art"

        result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=False)
        assert result == {"success": True, "removed_count": 0}
        assert store_art in file_store.files

    @pytest.mark.asyncio
    async def test_live_foreign_shortcut_art_kept(self, artwork_service, steam_config, file_store, tmp_path):
        """A live (non-RomM) shortcut's art is protected by the submitted keep-set."""
        grid = self._grid(steam_config, tmp_path)
        foreign = os.path.join(grid, f"{self.FOREIGN}p.png")
        orphan = os.path.join(grid, f"{self.ORPHAN}_hero.jpg")
        file_store.files[foreign] = b"foreign"
        file_store.files[orphan] = b"orphan"

        result = await artwork_service.cleanup_orphaned_grid_images([self.FOREIGN], dry_run=False)
        assert result == {"success": True, "removed_count": 1}
        assert foreign in file_store.files
        assert orphan not in file_store.files

    @pytest.mark.asyncio
    async def test_dry_run_counts_without_deleting(self, artwork_service, steam_config, file_store, tmp_path):
        grid = self._grid(steam_config, tmp_path)
        orphan_a = os.path.join(grid, f"{self.ORPHAN}p.png")
        orphan_b = os.path.join(grid, f"{self.ORPHAN}.png")
        file_store.files[orphan_a] = b"a"
        file_store.files[orphan_b] = b"b"

        result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=True)
        assert result == {"success": True, "candidate_count": 2}
        assert orphan_a in file_store.files
        assert orphan_b in file_store.files

    @pytest.mark.asyncio
    async def test_incomplete_scan_refused_and_nothing_deleted(
        self, artwork_service, uow, steam_config, file_store, tmp_path
    ):
        """A bound shortcut missing from the live set proves the scan incomplete."""
        grid = self._grid(steam_config, tmp_path)
        _seed_rom(uow, 42, app_id=self.BOUND)
        orphan = os.path.join(grid, f"{self.ORPHAN}p.png")
        file_store.files[orphan] = b"orphan"

        # Live set omits the bound appId — refuse, delete nothing.
        result = await artwork_service.cleanup_orphaned_grid_images([self.FOREIGN], dry_run=False)
        assert result["success"] is False
        assert result["reason"] == "incomplete_scan"
        assert isinstance(result["message"], str) and result["message"]
        assert orphan in file_store.files

    @pytest.mark.asyncio
    async def test_bound_app_id_present_in_live_set_passes_guard(
        self, artwork_service, uow, steam_config, file_store, tmp_path
    ):
        grid = self._grid(steam_config, tmp_path)
        _seed_rom(uow, 42, app_id=self.BOUND)
        bound_art = os.path.join(grid, f"{self.BOUND}p.png")
        orphan = os.path.join(grid, f"{self.ORPHAN}p.png")
        file_store.files[bound_art] = b"bound"
        file_store.files[orphan] = b"orphan"

        result = await artwork_service.cleanup_orphaned_grid_images([self.BOUND], dry_run=False)
        assert result == {"success": True, "removed_count": 1}
        assert bound_art in file_store.files
        assert orphan not in file_store.files

    @pytest.mark.asyncio
    async def test_no_grid_dir_fails(self, artwork_service, steam_config):
        steam_config.grid_dir.return_value = None
        result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=True)
        assert result == {
            "success": False,
            "reason": "no_grid_dir",
            "message": "Steam grid directory not found",
        }

    @pytest.mark.asyncio
    async def test_grid_not_a_directory_fails(self, artwork_service, steam_config, file_store, tmp_path):
        self._grid(steam_config, tmp_path)
        file_store.isdir_paths = set()  # grid path exists as a value but is not a dir
        result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=True)
        assert result["success"] is False
        assert result["reason"] == "no_grid_dir"

    @pytest.mark.asyncio
    async def test_empty_live_set_with_zero_bindings_is_legal(
        self, artwork_service, steam_config, file_store, tmp_path
    ):
        """No bound ROMs + no live shortcuts: every in-range grid image is orphaned."""
        grid = self._grid(steam_config, tmp_path)
        orphan = os.path.join(grid, f"{self.ORPHAN}_logo.jpeg")
        store_art = os.path.join(grid, "570.png")
        file_store.files[orphan] = b"orphan"
        file_store.files[store_art] = b"store"

        result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=False)
        assert result == {"success": True, "removed_count": 1}
        assert orphan not in file_store.files
        assert store_art in file_store.files

    @pytest.mark.asyncio
    async def test_all_grid_forms_of_orphan_removed(self, artwork_service, steam_config, file_store, tmp_path):
        grid = self._grid(steam_config, tmp_path)
        staged = [
            os.path.join(grid, f"{self.ORPHAN}{suffix}.{ext}")
            for suffix in ("p", "_hero", "_logo", "_icon", "")
            for ext in ("png", "jpg", "jpeg")
        ]
        for path in staged:
            file_store.files[path] = b"art"

        result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=False)
        assert result == {"success": True, "removed_count": 15}
        for path in staged:
            assert path not in file_store.files

    @pytest.mark.asyncio
    async def test_non_grid_image_names_untouched(self, artwork_service, steam_config, file_store, tmp_path):
        grid = self._grid(steam_config, tmp_path)
        staging = os.path.join(grid, "romm_42_cover.png")
        sidecar = os.path.join(grid, f"{self.ORPHAN}p.png.tmp")
        junk = os.path.join(grid, "notes.txt")
        for path in (staging, sidecar, junk):
            file_store.files[path] = b"keep"

        result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=False)
        assert result == {"success": True, "removed_count": 0}
        for path in (staging, sidecar, junk):
            assert path in file_store.files

    @pytest.mark.asyncio
    async def test_directory_entry_skipped(self, artwork_service, steam_config, file_store, tmp_path):
        """A grid-image-named DIRECTORY is never deleted (files only)."""
        grid = self._grid(steam_config, tmp_path)
        nested = os.path.join(grid, f"{self.ORPHAN}p.png", "inner")
        file_store.files[nested] = b"inside a dir"

        result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=False)
        assert result == {"success": True, "removed_count": 0}
        assert nested in file_store.files

    @pytest.mark.asyncio
    async def test_remove_failure_logged_and_run_continues(
        self, artwork_service, steam_config, file_store, tmp_path, caplog
    ):
        grid = self._grid(steam_config, tmp_path)
        failing = os.path.join(grid, f"{self.ORPHAN}p.png")
        removable = os.path.join(grid, f"{self.ORPHAN}_hero.png")
        file_store.files[failing] = b"stuck"
        file_store.files[removable] = b"ok"

        real_remove = file_store.remove_file

        def selective_boom(path: str) -> None:
            if path == failing:
                raise OSError("permission denied")
            real_remove(path)

        file_store.remove_file = selective_boom  # type: ignore[method-assign]

        with caplog.at_level(logging.WARNING):
            result = await artwork_service.cleanup_orphaned_grid_images([], dry_run=False)

        assert result == {"success": True, "removed_count": 1}
        assert failing in file_store.files
        assert removable not in file_store.files
        assert any("Failed to remove orphaned grid image" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_scan_and_delete_offloaded_to_executor(self, artwork_service, steam_config, file_store, tmp_path):
        """The directory scan + deletions run via run_in_executor, not on the loop."""
        grid = self._grid(steam_config, tmp_path)
        file_store.files[os.path.join(grid, f"{self.ORPHAN}p.png")] = b"orphan"

        loop = MagicMock()
        loop.run_in_executor = AsyncMock(return_value={"success": True, "candidate_count": 1})
        artwork_service._loop = loop

        result = await artwork_service.cleanup_orphaned_grid_images([self.FOREIGN], dry_run=True)
        assert result == {"success": True, "candidate_count": 1}
        loop.run_in_executor.assert_awaited_once_with(
            None,
            artwork_service._cleanup_orphaned_grid_images_io,
            grid,
            {self.FOREIGN},
            True,
        )


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
