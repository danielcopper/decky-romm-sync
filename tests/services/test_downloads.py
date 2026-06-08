import asyncio
import os
import sqlite3
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from conftest import _make_testable_plugin
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.library_peers import FakeArtworkManager
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.download_file import DownloadFileAdapter
from adapters.rom_files import RomFileAdapter
from adapters.steam_config import SteamConfigAdapter
from domain.rom import Rom
from domain.rom_install import RomInstall
from services.downloads import DownloadService, DownloadServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.rom_removal import RomRemovalService, RomRemovalServiceConfig


def _seed_rom(uow: FakeUnitOfWork, rom_id: int, *, platform_slug: str = "n64") -> None:
    """Seed a synced ``Rom`` so a ``RomInstall`` save passes the FK check at commit."""
    uow.roms.save(
        Rom.synced(
            rom_id=rom_id,
            platform_slug=platform_slug,
            name=f"Game {rom_id}",
            fs_name=f"game_{rom_id}.z64",
            shortcut_app_id=1000 + rom_id,
            synced_at="2026-01-01T00:00:00+00:00",
        )
    )


def _seed_install(
    uow: FakeUnitOfWork,
    rom_id: int,
    *,
    file_path: str,
    rom_dir: str | None = None,
    system: str = "n64",
) -> None:
    """Seed the FK-parent ``Rom`` THEN its ``RomInstall`` record, in one commit."""
    with uow:
        _seed_rom(uow, rom_id, platform_slug=system)
        uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=file_path,
                rom_dir=rom_dir,
                platform_slug=system,
                system=system,
                installed_at="2026-01-01T00:00:00+00:00",
            )
        )


@pytest.fixture
def plugin():
    p = _make_testable_plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()
    p._resolve_system = MagicMock(side_effect=lambda slug, fs_slug=None: fs_slug or slug)

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=p._romm_api,
            steam_config=steam_config,
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            settings_persister=MagicMock(),
            log_debug=p._log_debug,
            artwork=FakeArtworkManager(),
            uow_factory=FakeUnitOfWorkFactory(),
            core_info=FakeCoreInfoProvider(),
            resolve_system=p._resolve_system,
        ),
    )
    # Shared fake Unit of Work — install records flow through it, and tests
    # inspect ``uow.rom_installs`` after the service has run. Exposed as
    # ``p._uow`` for assertions.
    p._uow = FakeUnitOfWork()
    # Shared core-info fake so a test can seed ``available_cores`` and assert the
    # per-game override re-bakes the ``-e`` form on download_complete.
    p._core_info = FakeCoreInfoProvider()
    p._download_service = DownloadService(
        config=DownloadServiceConfig(
            romm_api=p._romm_api,
            download_file_store=DownloadFileAdapter(),
            resolve_system=p._resolve_system,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            emit=decky.emit,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            sleeper=FakeSleeper(),
            retrodeck_paths=FakeRetroDeckPaths(
                roms=os.path.join(os.path.expanduser("~"), "retrodeck", "roms"),
                bios=os.path.join(os.path.expanduser("~"), "retrodeck", "bios"),
            ),
            core_info=p._core_info,
            uow_factory=FakeUnitOfWorkFactory(p._uow),
        ),
    )
    p._rom_removal_service = RomRemovalService(
        config=RomRemovalServiceConfig(
            logger=decky.logger,
            loop=asyncio.get_event_loop(),
            rom_file_store=RomFileAdapter(),
            retrodeck_paths=FakeRetroDeckPaths(
                roms=os.path.join(os.path.expanduser("~"), "retrodeck", "roms"),
            ),
            download_queue_cleanup=p._download_service,
            uow_factory=FakeUnitOfWorkFactory(p._uow),
        ),
    )
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop matches the running event loop for async tests."""
    plugin.loop = asyncio.get_event_loop()
    plugin._download_service._loop = asyncio.get_event_loop()
    plugin._rom_removal_service._loop = asyncio.get_event_loop()


class TestStartDownload:
    @pytest.mark.asyncio
    async def test_starts_download_task(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "fs_size_bytes": 1024,
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)
        _create_task_calls = []

        def _close_coro_task(coro):
            coro.close()
            _create_task_calls.append(coro)
            return MagicMock()

        plugin._download_service._loop.create_task = _close_coro_task
        plugin._download_service._download_file_store.disk_free = lambda _path: 500 * 1024 * 1024

        result = await plugin.start_download(42)

        assert result["success"] is True
        assert 42 in plugin._download_service._download_queue
        assert plugin._download_service._download_queue[42]["status"] == "downloading"
        assert len(_create_task_calls) == 1

    @pytest.mark.asyncio
    async def test_rejects_already_downloading(self, plugin):
        plugin._download_service._download_in_progress.add(42)
        result = await plugin.start_download(42)
        assert result["success"] is False
        assert "Already downloading" in result["message"]

    @pytest.mark.asyncio
    async def test_rejects_if_rom_not_found(self, plugin):
        from unittest.mock import AsyncMock

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(side_effect=Exception("HTTP Error 404: Not Found"))

        result = await plugin.start_download(9999)
        assert result["success"] is False
        assert "error_code" in result

    @pytest.mark.asyncio
    async def test_checks_disk_space(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "fs_size_bytes": 500 * 1024 * 1024,  # 500MB
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)

        plugin._download_service._download_file_store.disk_free = lambda _path: 50 * 1024 * 1024
        result = await plugin.start_download(42)

        assert result["success"] is False
        assert "disk space" in result["message"].lower()


class TestCancelDownload:
    @pytest.mark.asyncio
    async def test_cancels_active_download(self, plugin):
        # Create a real future that raises CancelledError when awaited
        loop = asyncio.get_event_loop()
        fut = loop.create_future()
        fut.cancel()

        plugin._download_service._download_tasks[42] = fut
        plugin._download_service._download_queue[42] = {"status": "downloading"}

        result = await plugin.cancel_download(42)
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_error(self, plugin):
        result = await plugin.cancel_download(999)
        assert result["success"] is False
        assert "No active download" in result["message"]


class TestGetDownloadQueue:
    @pytest.mark.asyncio
    async def test_returns_empty_queue(self, plugin):
        result = await plugin.get_download_queue()
        assert result["downloads"] == []

    @pytest.mark.asyncio
    async def test_returns_active_downloads(self, plugin):
        plugin._download_service._download_queue[1] = {
            "rom_id": 1,
            "rom_name": "Game A",
            "status": "downloading",
            "progress": 0.5,
        }
        result = await plugin.get_download_queue()
        assert len(result["downloads"]) == 1
        assert result["downloads"][0]["status"] == "downloading"
        assert result["downloads"][0]["progress"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_returns_completed_downloads(self, plugin):
        plugin._download_service._download_queue[1] = {
            "rom_id": 1,
            "rom_name": "Game A",
            "status": "downloading",
            "progress": 0.5,
        }
        plugin._download_service._download_queue[2] = {
            "rom_id": 2,
            "rom_name": "Game B",
            "status": "completed",
            "progress": 1.0,
        }
        result = await plugin.get_download_queue()
        assert len(result["downloads"]) == 2
        statuses = {d["status"] for d in result["downloads"]}
        assert statuses == {"downloading", "completed"}


class TestGetInstalledRom:
    @pytest.mark.asyncio
    async def test_returns_installed_rom(self, plugin):
        _seed_rom(plugin._uow, 42)
        plugin._uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=42,
                file_path="/roms/n64/zelda.z64",
                rom_dir=None,
                platform_slug="n64",
                system="n64",
                installed_at="2026-01-01T00:00:00+00:00",
            )
        )
        result = await plugin.get_installed_rom(42)
        assert result is not None
        assert result["rom_id"] == 42
        assert result["system"] == "n64"
        # file_name is derived from the launch file_path.
        assert result["file_name"] == "zelda.z64"
        assert result["file_path"] == "/roms/n64/zelda.z64"
        assert result["platform_slug"] == "n64"

    @pytest.mark.asyncio
    async def test_returns_none_not_installed(self, plugin):
        result = await plugin.get_installed_rom(999)
        assert result is None


class TestRomInstallForeignKey:
    """A RomInstall whose rom_id has no synced Rom is rejected at commit.

    Mirrors the schema's ``rom_installs.rom_id REFERENCES roms(rom_id)`` under
    ``PRAGMA foreign_keys=ON`` — the FakeUnitOfWork enforces it on commit so the
    install slice can't silently persist an orphan.
    """

    def test_orphan_install_save_raises_integrity_error_at_commit(self, plugin):
        uow = plugin._uow
        with pytest.raises(sqlite3.IntegrityError, match="rom_installs"), uow:
            uow.rom_installs.save(
                RomInstall.mark_installed(
                    rom_id=42,  # no matching roms row seeded
                    file_path="/roms/n64/zelda.z64",
                    rom_dir=None,
                    platform_slug="n64",
                    system="n64",
                    installed_at="2026-01-01T00:00:00+00:00",
                )
            )
        assert uow.committed is False


class TestRemoveRom:
    @pytest.mark.asyncio
    async def test_deletes_file_and_clears_state(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_file = tmp_path / "retrodeck" / "roms" / "n64" / "zelda.z64"
        rom_file.parent.mkdir(parents=True)
        rom_file.write_text("fake rom data")

        _seed_install(
            plugin._uow,
            42,
            file_path=str(rom_file),
            rom_dir=None,
        )
        plugin._download_service._download_queue[42] = {"status": "completed"}

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        assert not rom_file.exists()
        assert plugin._uow.rom_installs.get(42) is None
        assert 42 not in plugin._download_service._download_queue

    @pytest.mark.asyncio
    async def test_returns_error_not_installed(self, plugin):
        result = await plugin.remove_rom(999)
        assert result["success"] is False
        assert "not installed" in result["message"].lower()


class TestUninstallAllRoms:
    @pytest.mark.asyncio
    async def test_removes_all_installed(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        file_a = roms_dir / "game_a.z64"
        file_b = roms_dir / "game_b.z64"
        file_a.write_text("data a")
        file_b.write_text("data b")

        _seed_install(plugin._uow, 1, file_path=str(file_a), rom_dir=None)
        _seed_install(plugin._uow, 2, file_path=str(file_b), rom_dir=None)

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 2
        assert not file_a.exists()
        assert not file_b.exists()

    @pytest.mark.asyncio
    async def test_clears_state(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        _seed_install(
            plugin._uow,
            1,
            file_path=str(tmp_path / "retrodeck" / "roms" / "n64" / "nonexistent.z64"),
            rom_dir=None,
        )

        await plugin.uninstall_all_roms()
        assert list(plugin._uow.rom_installs.iter_all()) == []

    @pytest.mark.asyncio
    async def test_handles_missing_files(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        roms_base = tmp_path / "retrodeck" / "roms"
        _seed_install(
            plugin._uow,
            1,
            file_path=str(roms_base / "n64" / "missing.z64"),
            rom_dir=None,
        )
        _seed_install(
            plugin._uow,
            2,
            file_path=str(roms_base / "snes" / "also_missing.z64"),
            rom_dir=None,
            system="snes",
        )

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        assert list(plugin._uow.rom_installs.iter_all()) == []


class TestDetectLaunchFile:
    def test_prefers_m3u(self, plugin, tmp_path):
        (tmp_path / "game.m3u").write_text("disc1.cue")
        (tmp_path / "disc1.cue").write_text("cue data")
        (tmp_path / "disc1.bin").write_bytes(b"\x00" * 1000)

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith(".m3u")

    def test_falls_back_to_cue(self, plugin, tmp_path):
        (tmp_path / "disc1.cue").write_text("cue data")
        (tmp_path / "disc1.bin").write_bytes(b"\x00" * 1000)

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith(".cue")

    def test_falls_back_to_largest(self, plugin, tmp_path):
        (tmp_path / "small.bin").write_bytes(b"\x00" * 100)
        (tmp_path / "large.bin").write_bytes(b"\x00" * 10000)

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith("large.bin")

    def test_wiiu_rpx_in_code_subdir(self, plugin, tmp_path):
        code_dir = tmp_path / "code"
        code_dir.mkdir()
        (code_dir / "game.rpx").write_bytes(b"\x00" * 500)
        (tmp_path / "meta" / "meta.xml").parent.mkdir()
        (tmp_path / "meta" / "meta.xml").write_text("<xml/>")

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith(".rpx")

    def test_wiiu_disc_image(self, plugin, tmp_path):
        (tmp_path / "game.wux").write_bytes(b"\x00" * 1000)
        (tmp_path / "readme.txt").write_text("info")

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith(".wux")

    def test_wiiu_wud_format(self, plugin, tmp_path):
        (tmp_path / "game.wud").write_bytes(b"\x00" * 1000)

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith(".wud")

    def test_wiiu_wua_format(self, plugin, tmp_path):
        (tmp_path / "game.wua").write_bytes(b"\x00" * 1000)

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith(".wua")

    def test_ps3_eboot_bin(self, plugin, tmp_path):
        usrdir = tmp_path / "PS3_GAME" / "USRDIR"
        usrdir.mkdir(parents=True)
        (usrdir / "EBOOT.BIN").write_bytes(b"\x00" * 500)
        (tmp_path / "PS3_GAME" / "PARAM.SFO").write_bytes(b"\x00" * 100)

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith("EBOOT.BIN")

    def test_3ds_prefers_3ds_over_cia(self, plugin, tmp_path):
        (tmp_path / "game.3ds").write_bytes(b"\x00" * 500)
        (tmp_path / "game.cia").write_bytes(b"\x00" * 500)

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith(".3ds")

    def test_3ds_falls_back_to_cia(self, plugin, tmp_path):
        (tmp_path / "game.cia").write_bytes(b"\x00" * 500)
        (tmp_path / "game.cxi").write_bytes(b"\x00" * 500)

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith(".cia")

    def test_m3u_still_preferred_over_platform_specific(self, plugin, tmp_path):
        """M3U takes priority even when platform-specific files exist."""
        (tmp_path / "game.m3u").write_text("disc1.cue")
        code_dir = tmp_path / "code"
        code_dir.mkdir()
        (code_dir / "game.rpx").write_bytes(b"\x00" * 500)

        result = plugin._download_service._collect_and_detect_launch_file(str(tmp_path))
        assert result.endswith(".m3u")


class TestDiskSpaceMultiFile:
    @pytest.mark.asyncio
    async def test_multi_file_rom_requires_double_space(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        file_size = 500 * 1024 * 1024  # 500MB
        rom_detail = {
            "id": 42,
            "name": "WiiU Game",
            "fs_name": "game.zip",
            "fs_size_bytes": file_size,
            "platform_slug": "wiiu",
            "platform_name": "Wii U",
            "has_multiple_files": True,
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)

        # 700MB free: enough for single-file (600MB) but not multi-file (1100MB)
        plugin._download_service._download_file_store.disk_free = lambda _path: 700 * 1024 * 1024
        result = await plugin.start_download(42)

        assert result["success"] is False
        assert "disk space" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_single_file_rom_uses_normal_space_check(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        file_size = 500 * 1024 * 1024  # 500MB
        rom_detail = {
            "id": 43,
            "name": "N64 Game",
            "fs_name": "game.z64",
            "fs_size_bytes": file_size,
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
            "has_multiple_files": False,
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)
        plugin._download_service._loop.create_task = MagicMock()

        # 700MB free: enough for single-file (600MB)
        plugin._download_service._download_file_store.disk_free = lambda _path: 700 * 1024 * 1024
        result = await plugin.start_download(43)

        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_nested_multi_file_rom_requires_double_space(self, plugin, tmp_path):
        """#855: nested-multi (has_multiple_files=False, len(files) > 1) reserves 2x."""
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        file_size = 500 * 1024 * 1024  # 500MB
        rom_detail = {
            "id": 44,
            "name": "Switch Game",
            "fs_name": "game.nsp",
            "fs_size_bytes": file_size,
            "platform_slug": "switch",
            "platform_name": "Nintendo Switch",
            "has_multiple_files": False,
            "has_nested_single_file": True,
            "files": [
                {"file_name": "game.nsp"},
                {"file_name": "update/patch.nsp"},
            ],
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)

        # 700MB free: enough for single-file (600MB) but not multi-file (1100MB).
        # If the gate only read has_multiple_files (False), this would pass —
        # the 2x reservation is what makes it fail.
        plugin._download_service._download_file_store.disk_free = lambda _path: 700 * 1024 * 1024
        result = await plugin.start_download(44)

        assert result["success"] is False
        assert "disk space" in result["message"].lower()


class TestMultiFileRomDeletion:
    @pytest.mark.asyncio
    async def test_remove_rom_deletes_rom_dir(self, plugin, tmp_path):
        """Multi-file ROM with rom_dir should delete the entire directory."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_dir = tmp_path / "retrodeck" / "roms" / "psx" / "FF7"
        rom_dir.mkdir(parents=True)
        (rom_dir / "FF7.m3u").write_text("disc1.cue")
        (rom_dir / "disc1.cue").write_text("cue")
        (rom_dir / "disc1.bin").write_bytes(b"\x00" * 100)

        _seed_install(
            plugin._uow,
            42,
            file_path=str(rom_dir / "FF7.m3u"),
            rom_dir=str(rom_dir),
            system="psx",
        )

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        assert not rom_dir.exists()
        # Parent system dir should still exist
        assert (tmp_path / "retrodeck" / "roms" / "psx").exists()

    @pytest.mark.asyncio
    async def test_uninstall_all_deletes_rom_dirs(self, plugin, tmp_path):
        """uninstall_all_roms should delete multi-file ROM directories."""
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_dir = tmp_path / "retrodeck" / "roms" / "psx" / "FF7"
        rom_dir.mkdir(parents=True)
        (rom_dir / "disc1.bin").write_bytes(b"\x00" * 100)

        _seed_install(
            plugin._uow,
            1,
            file_path=str(rom_dir / "FF7.m3u"),
            rom_dir=str(rom_dir),
            system="psx",
        )

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 1
        assert not rom_dir.exists()


class TestMaybeGenerateM3u:
    def test_generates_m3u_for_multiple_cue_files(self, plugin, tmp_path):
        """When multiple .cue files exist and no .m3u, auto-generate one."""
        (tmp_path / "Game - Disc 1.cue").write_text("cue disc 1")
        (tmp_path / "Game - Disc 1.bin").write_bytes(b"\x00" * 1000)
        (tmp_path / "Game - Disc 2.cue").write_text("cue disc 2")
        (tmp_path / "Game - Disc 2.bin").write_bytes(b"\x00" * 1000)

        rom_detail = {"fs_name_no_ext": "Final Fantasy VII", "name": "Final Fantasy VII"}
        plugin._download_service._maybe_generate_m3u_io(str(tmp_path), rom_detail)

        m3u_path = tmp_path / "Final Fantasy VII.m3u"
        assert m3u_path.exists()
        content = m3u_path.read_text()
        lines = content.strip().split("\n")
        assert len(lines) == 2
        assert lines[0] == "Game - Disc 1.cue"
        assert lines[1] == "Game - Disc 2.cue"

    def test_generates_m3u_for_multiple_chd_files(self, plugin, tmp_path):
        """CHD multi-disc should also get an M3U."""
        (tmp_path / "Game (Disc 1).chd").write_bytes(b"\x00" * 100)
        (tmp_path / "Game (Disc 2).chd").write_bytes(b"\x00" * 100)

        rom_detail = {"fs_name_no_ext": "Game", "name": "Game"}
        plugin._download_service._maybe_generate_m3u_io(str(tmp_path), rom_detail)

        m3u_path = tmp_path / "Game.m3u"
        assert m3u_path.exists()
        lines = m3u_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_skips_if_m3u_exists(self, plugin, tmp_path):
        """Should not overwrite an existing M3U."""
        (tmp_path / "existing.m3u").write_text("original content")
        (tmp_path / "disc1.cue").write_text("cue 1")
        (tmp_path / "disc2.cue").write_text("cue 2")

        rom_detail = {"fs_name_no_ext": "Game"}
        plugin._download_service._maybe_generate_m3u_io(str(tmp_path), rom_detail)

        # Only the original M3U should exist, unchanged
        assert (tmp_path / "existing.m3u").read_text() == "original content"
        assert not (tmp_path / "Game.m3u").exists()

    def test_skips_single_disc(self, plugin, tmp_path):
        """Single disc file should not generate an M3U."""
        (tmp_path / "game.cue").write_text("cue data")
        (tmp_path / "game.bin").write_bytes(b"\x00" * 1000)

        rom_detail = {"fs_name_no_ext": "Game"}
        plugin._download_service._maybe_generate_m3u_io(str(tmp_path), rom_detail)

        assert not (tmp_path / "Game.m3u").exists()

    def test_uses_name_fallback(self, plugin, tmp_path):
        """Falls back to rom name when fs_name_no_ext is missing."""
        (tmp_path / "d1.chd").write_bytes(b"\x00" * 100)
        (tmp_path / "d2.chd").write_bytes(b"\x00" * 100)

        rom_detail = {"name": "My Game"}
        plugin._download_service._maybe_generate_m3u_io(str(tmp_path), rom_detail)

        assert (tmp_path / "My Game.m3u").exists()


class TestDoDownloadSingleFile:
    """Tests for _do_download happy path — single file."""

    @pytest.mark.asyncio
    async def test_single_file_happy_path(self, plugin, tmp_path):
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "zelda.z64")

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
            "has_multiple_files": False,
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(b"\x00" * 512)

        _seed_rom(plugin._uow, 42)
        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[42] = {"rom_id": 42, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(42, rom_detail, target_path, "n64", "zelda.z64")

        # File ends up at target_path (not .tmp)
        assert os.path.exists(target_path)
        assert not os.path.exists(target_path + ".tmp")
        # RomInstall record persisted via the Unit of Work.
        installed = plugin._uow.rom_installs.get(42)
        assert installed is not None
        assert installed.rom_id == 42
        assert installed.file_path == target_path
        # Single-file ROM owns no dedicated folder.
        assert installed.rom_dir is None
        assert installed.system == "n64"
        assert installed.platform_slug == "n64"
        assert installed.installed_at
        # download_complete event emitted
        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "download_complete"]
        assert len(emit_calls) == 1
        payload = emit_calls[0][0][1]
        assert payload["rom_id"] == 42
        assert payload["file_path"] == target_path
        # app_id carries the ROM's bound shortcut_app_id (seeded as 1000 + rom_id)
        # so the frontend confirm-sets launch options without a full-library scan.
        assert payload["app_id"] == 1042
        # launch_options carries the full RetroDECK launch command for the resolved path.
        assert payload["launch_options"] == f'flatpak run net.retrodeck.retrodeck "{target_path}"'
        # download_queue status is completed
        assert plugin._download_service._download_queue[42]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_download_complete_app_id_null_when_unbound(self, plugin, tmp_path):
        """A ROM downloaded before it's synced (no Steam shortcut) emits ``app_id: None``.

        The ROM row exists (FK parent for the install) but its
        ``shortcut_app_id`` is ``None`` — so ``download_complete`` carries
        ``app_id == None`` and the frontend handler no-ops gracefully.
        """
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "metroid.z64")

        rom_detail = {
            "id": 7,
            "name": "Metroid",
            "fs_name": "metroid.z64",
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
            "has_multiple_files": False,
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(b"\x00" * 512)

        # Unbound ROM row (FK parent for the install) — no Steam shortcut yet.
        with plugin._uow:
            plugin._uow.roms.save(
                Rom(
                    rom_id=7,
                    platform_slug="n64",
                    name="Metroid",
                    fs_name="metroid.z64",
                    shortcut_app_id=None,
                    last_synced_at="2025-01-01T00:00:00",
                )
            )
        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[7] = {"rom_id": 7, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(7, rom_detail, target_path, "n64", "metroid.z64")

        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "download_complete"]
        assert len(emit_calls) == 1
        assert emit_calls[0][0][1]["app_id"] is None


class TestDoDownloadOverrideRebake:
    """``download_complete`` re-bakes a per-game ``emulator_override`` into launch_options.

    This is the load-bearing site (B2): the override lives on ``roms`` precisely so
    it survives uninstall → reinstall, and reinstall flows through ``_do_download``.
    """

    async def _run_single_download(self, plugin, tmp_path, *, rom_id, override):
        """Download one single-file ROM (bound) with ``override`` pre-pinned; return payload."""
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "game.chd")
        rom_detail = {
            "id": rom_id,
            "name": "PSX Game",
            "fs_name": "game.chd",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": False,
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(b"\x00" * 512)

        _seed_rom(plugin._uow, rom_id, platform_slug="psx")
        if override is not None:
            with plugin._uow:
                plugin._uow.roms.set_emulator_override(rom_id, override)
        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[rom_id] = {"rom_id": rom_id, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(rom_id, rom_detail, target_path, "psx", "game.chd")

        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "download_complete"]
        assert len(emit_calls) == 1
        return emit_calls[0][0][1], target_path

    @pytest.mark.asyncio
    async def test_reinstall_with_override_rebakes_e_form(self, plugin, tmp_path):
        """An override-set ROM's reinstall emits ``-e`` baked launch_options (B2)."""
        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]
        payload, target_path = await self._run_single_download(plugin, tmp_path, rom_id=42, override="PCSX ReARMed")
        assert payload["app_id"] == 1042
        assert payload["launch_options"] == (
            "flatpak run net.retrodeck.retrodeck "
            '-e "%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/pcsx_rearmed_libretro.so %ROM%" '
            f'"{target_path}"'
        )

    @pytest.mark.asyncio
    async def test_reinstall_without_override_is_plain(self, plugin, tmp_path):
        """A NULL-override ROM's reinstall emits the plain launch — no ``-e`` (B2)."""
        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]
        payload, target_path = await self._run_single_download(plugin, tmp_path, rom_id=43, override=None)
        assert payload["launch_options"] == f'flatpak run net.retrodeck.retrodeck "{target_path}"'
        assert "-e" not in payload["launch_options"]

    @pytest.mark.asyncio
    async def test_reinstall_with_stale_override_rebakes_plain_and_warns(self, plugin, tmp_path, caplog):
        """A stale override LABEL reinstall emits the PLAIN launch + WARNs (B4)."""
        import logging

        # available_cores does not carry the pinned label → resolution returns None.
        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]
        with caplog.at_level(logging.WARNING):
            payload, target_path = await self._run_single_download(plugin, tmp_path, rom_id=44, override="Removed Core")
        assert payload["launch_options"] == f'flatpak run net.retrodeck.retrodeck "{target_path}"'
        assert "-e" not in payload["launch_options"]
        assert "Removed Core" in caplog.text
        assert "no longer resolves" in caplog.text


class TestDoDownloadMultiFile:
    """Tests for _do_download happy path — multi-file (ZIP)."""

    @pytest.mark.asyncio
    async def test_multi_file_happy_path(self, plugin, tmp_path):
        import zipfile as zf
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "FF7.zip")

        # Create a real ZIP file that our fake download will write
        zip_content_path = tmp_path / "source.zip"
        with zf.ZipFile(str(zip_content_path), "w") as z:
            z.writestr("disc1.cue", "FILE disc1.bin BINARY")
            z.writestr("disc1.bin", b"\x00" * 100)
            z.writestr("disc2.cue", "FILE disc2.bin BINARY")
            z.writestr("disc2.bin", b"\x00" * 100)
        zip_bytes = zip_content_path.read_bytes()

        rom_detail = {
            "id": 55,
            "name": "Final Fantasy VII",
            "fs_name": "FF7.zip",
            "fs_name_no_ext": "FF7",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": True,
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(zip_bytes)

        _seed_rom(plugin._uow, 55, platform_slug="psx")
        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[55] = {"rom_id": 55, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(55, rom_detail, target_path, "psx", "FF7.zip")

        # ZIP is extracted to extract_dir
        extract_dir = roms_dir / "FF7"
        assert extract_dir.is_dir()
        assert (extract_dir / "disc1.cue").exists()
        assert (extract_dir / "disc2.cue").exists()
        # .zip.tmp is cleaned up
        assert not os.path.exists(target_path + ".zip.tmp")
        # RomInstall record has rom_dir pointing at the extracted dir.
        installed = plugin._uow.rom_installs.get(55)
        assert installed is not None
        assert installed.rom_dir == str(extract_dir)
        # Launch file detection: M3U generated from 2 cue files, so prefer M3U > CUE
        # (M3U auto-generated by _maybe_generate_m3u)
        assert installed.file_path.endswith((".m3u", ".cue"))
        # download_complete carries the launch command for the detected launch file.
        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "download_complete"]
        assert len(emit_calls) == 1
        payload = emit_calls[0][0][1]
        assert payload["file_path"] == installed.file_path
        assert payload["launch_options"] == f'flatpak run net.retrodeck.retrodeck "{installed.file_path}"'
        # Status is completed
        assert plugin._download_service._download_queue[55]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_nested_multi_file_takes_extract_path(self, plugin, tmp_path):
        """#855: one top-level file but len(files) > 1 → RomM zips → EXTRACT path.

        Switch base/update/DLC: ``has_multiple_files=False`` (single top-level
        file) yet ``len(files) > 1``. RomM streams a mod_zip ZIP, so the plugin
        must extract it into a per-game folder instead of writing the ZIP bytes
        verbatim into one .nsp.
        """
        import zipfile as zf
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "switch"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "Zelda.nsp")

        # Real ZIP mirroring RomM's mod_zip output: base at root + nested update/DLC
        zip_content_path = tmp_path / "source.zip"
        with zf.ZipFile(str(zip_content_path), "w") as z:
            z.writestr("Zelda.nsp", b"\x00" * 100)
            z.writestr("update/Zelda_update.nsp", b"\x00" * 100)
            z.writestr("dlc/Zelda_dlc.nsp", b"\x00" * 100)
        zip_bytes = zip_content_path.read_bytes()

        rom_detail = {
            "id": 99,
            "name": "Zelda",
            "fs_name": "Zelda.nsp",
            "fs_name_no_ext": "Zelda",
            "platform_slug": "switch",
            "platform_name": "Nintendo Switch",
            "has_multiple_files": False,
            "has_nested_single_file": True,
            "files": [
                {"file_name": "Zelda.nsp"},
                {"file_name": "update/Zelda_update.nsp"},
                {"file_name": "dlc/Zelda_dlc.nsp"},
            ],
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(zip_bytes)

        _seed_rom(plugin._uow, 99, platform_slug="switch")
        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[99] = {"rom_id": 99, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(99, rom_detail, target_path, "switch", "Zelda.nsp")

        # ZIP is extracted into a per-game folder (not written verbatim into one .nsp)
        extract_dir = roms_dir / "Zelda"
        assert extract_dir.is_dir()
        assert (extract_dir / "Zelda.nsp").exists()
        assert (extract_dir / "update" / "Zelda_update.nsp").exists()
        assert (extract_dir / "dlc" / "Zelda_dlc.nsp").exists()
        # The verbatim single-file artifact must NOT exist
        assert not os.path.exists(target_path)
        # .zip.tmp is cleaned up
        assert not os.path.exists(target_path + ".zip.tmp")
        # RomInstall record registers rom_dir (extract path), and the launch
        # file_path points inside it — not a flat file written verbatim.
        installed = plugin._uow.rom_installs.get(99)
        assert installed is not None
        assert installed.rom_dir == str(extract_dir)
        assert installed.file_path.startswith(str(extract_dir) + os.sep)
        assert plugin._download_service._download_queue[99]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_nested_multi_file_cleanup_removes_extract_dir(self, plugin, tmp_path):
        """#855: a nested-multi download failure must remove the extract dir.

        The partial-download cleanup keys on the same multi-file gate, so a
        ZIP-extraction failure for a nested-multi ROM must tear down the
        per-game folder (the 2x-reservation multi-file branch), not leave it.
        """
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "switch"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "Zelda.nsp")

        # Pre-seed an extract dir as if extraction had partially happened.
        extract_dir = roms_dir / "Zelda"
        extract_dir.mkdir()
        (extract_dir / "Zelda.nsp").write_bytes(b"\x00" * 100)

        rom_detail = {
            "id": 99,
            "name": "Zelda",
            "fs_name": "Zelda.nsp",
            "fs_name_no_ext": "Zelda",
            "platform_slug": "switch",
            "platform_name": "Nintendo Switch",
            "has_multiple_files": False,
            "has_nested_single_file": True,
            "files": [
                {"file_name": "Zelda.nsp"},
                {"file_name": "update/Zelda_update.nsp"},
            ],
        }

        def fake_download(_rom_id, _filename, _dest, _progress_callback=None):
            raise OSError("network died mid-download")

        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[99] = {"rom_id": 99, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(99, rom_detail, target_path, "switch", "Zelda.nsp")

        # Failure path keyed on the multi-file gate → extract dir torn down.
        assert not extract_dir.exists()
        assert plugin._download_service._download_queue[99]["status"] == "failed"


class TestDoDownloadNestedSingleFile:
    """Tests for has_nested_single_file: fs_name is the parent folder, not the file (#226)."""

    @pytest.mark.asyncio
    async def test_simple_single_file_unchanged(self, plugin, tmp_path):
        """Regression: simple-single-file still uses fs_name as the local filename."""
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        roms_dir = tmp_path / "retrodeck" / "roms" / "gba"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "Game.gba")

        rom_detail = {
            "id": 1,
            "name": "Game",
            "fs_name": "Game.gba",
            "platform_slug": "gba",
            "platform_name": "Game Boy Advance",
            "has_simple_single_file": True,
            "has_nested_single_file": False,
            "has_multiple_files": False,
            "files": [{"file_name": "Game.gba"}],
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(b"\x00" * 64)

        _seed_rom(plugin._uow, 1, platform_slug="gba")
        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[1] = {"rom_id": 1, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(1, rom_detail, target_path, "gba", "Game.gba")

        assert os.path.exists(target_path)
        installed = plugin._uow.rom_installs.get(1)
        assert installed is not None
        assert os.path.basename(installed.file_path) == "Game.gba"
        assert installed.file_path == target_path

    @pytest.mark.asyncio
    async def test_nested_single_file_uses_files_entry(self, plugin, tmp_path):
        """Happy path: has_nested_single_file derives the local filename from files[0].file_name."""
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        roms_dir = tmp_path / "retrodeck" / "roms" / "dc"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "My Game.chd")

        rom_detail = {
            "id": 7,
            "name": "My Game",
            "fs_name": "My Game",
            "platform_slug": "dc",
            "platform_name": "Dreamcast",
            "has_nested_single_file": True,
            "has_multiple_files": False,
            "files": [{"file_name": "My Game.chd"}],
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(b"\x00" * 128)

        _seed_rom(plugin._uow, 7, platform_slug="dc")
        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[7] = {"rom_id": 7, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(7, rom_detail, target_path, "dc", "My Game.chd")

        assert os.path.exists(target_path)
        installed = plugin._uow.rom_installs.get(7)
        assert installed is not None
        assert os.path.basename(installed.file_path) == "My Game.chd"
        assert installed.file_path == target_path
        # Must NOT keep the parent-folder name from fs_name as a real on-disk file
        assert not os.path.exists(str(roms_dir / "My Game"))
        # #855 regression: a genuine nested-single ROM (len(files) == 1) must
        # stay on the single-file path — it flattens to a flat file and never
        # registers an extract directory.
        assert installed.rom_dir is None

    @pytest.mark.asyncio
    async def test_nested_single_file_start_download_uses_files_entry(self, plugin, tmp_path):
        """start_download: nested-single-file enters the queue with the resolved filename."""
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_detail = {
            "id": 7,
            "name": "Resident Evil",
            "fs_name": "Resident Evil",
            "fs_size_bytes": 1024,
            "platform_slug": "dc",
            "platform_name": "Dreamcast",
            "has_nested_single_file": True,
            "has_multiple_files": False,
            "files": [{"file_name": "Resident Evil.chd"}],
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)

        def _close_coro_task(coro):
            coro.close()
            return MagicMock()

        plugin._download_service._loop.create_task = _close_coro_task

        plugin._download_service._download_file_store.disk_free = lambda _path: 500 * 1024 * 1024
        result = await plugin.start_download(7)

        assert result["success"] is True
        assert plugin._download_service._download_queue[7]["file_name"] == "Resident Evil.chd"

    @pytest.mark.asyncio
    async def test_nested_single_file_empty_files_falls_back(self, plugin, tmp_path, caplog):
        """Defensive: empty files list falls back to fs_name and logs a warning."""
        import logging
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_detail = {
            "id": 8,
            "name": "My Game",
            "fs_name": "My Game",
            "fs_size_bytes": 1024,
            "platform_slug": "dc",
            "platform_name": "Dreamcast",
            "has_nested_single_file": True,
            "has_multiple_files": False,
            "files": [],
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)

        def _close_coro_task(coro):
            coro.close()
            return MagicMock()

        plugin._download_service._loop.create_task = _close_coro_task
        plugin._download_service._download_file_store.disk_free = lambda _path: 500 * 1024 * 1024

        with caplog.at_level(logging.WARNING, logger="test_romm"):
            result = await plugin.start_download(8)

        assert result["success"] is True
        assert plugin._download_service._download_queue[8]["file_name"] == "My Game"
        assert any("has_nested_single_file" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_nested_single_file_missing_files_key_falls_back(self, plugin, tmp_path, caplog):
        """Defensive: missing files key falls back to fs_name and logs a warning."""
        import logging
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_detail = {
            "id": 9,
            "name": "My Game",
            "fs_name": "My Game",
            "fs_size_bytes": 1024,
            "platform_slug": "dc",
            "platform_name": "Dreamcast",
            "has_nested_single_file": True,
            "has_multiple_files": False,
            # no "files" key at all
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)

        def _close_coro_task(coro):
            coro.close()
            return MagicMock()

        plugin._download_service._loop.create_task = _close_coro_task
        plugin._download_service._download_file_store.disk_free = lambda _path: 500 * 1024 * 1024

        with caplog.at_level(logging.WARNING, logger="test_romm"):
            result = await plugin.start_download(9)

        assert result["success"] is True
        assert plugin._download_service._download_queue[9]["file_name"] == "My Game"
        assert any("has_nested_single_file" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_nested_single_file_traversal_sanitized(self, plugin, tmp_path):
        """Defensive: path traversal in files[0].file_name is sanitized via os.path.basename."""
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_detail = {
            "id": 13,
            "name": "Evil Nested",
            "fs_name": "Evil",
            "fs_size_bytes": 1024,
            "platform_slug": "dc",
            "platform_name": "Dreamcast",
            "has_nested_single_file": True,
            "has_multiple_files": False,
            "files": [{"file_name": "../evil.chd"}],
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)

        def _close_coro_task(coro):
            coro.close()
            return MagicMock()

        plugin._download_service._loop.create_task = _close_coro_task

        plugin._download_service._download_file_store.disk_free = lambda _path: 500 * 1024 * 1024
        result = await plugin.start_download(13)

        assert result["success"] is True
        queue_entry = plugin._download_service._download_queue[13]
        assert queue_entry["file_name"] == "evil.chd"
        assert ".." not in queue_entry["file_name"]


class TestPathTraversalDeleteRomFiles:
    """Tests for path traversal safety in _delete_rom_files."""

    @pytest.mark.asyncio
    async def test_rejects_rom_dir_outside_roms_base(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        # Create a file outside roms dir that should NOT be deleted
        evil_dir = tmp_path / "evil"
        evil_dir.mkdir()
        evil_file = evil_dir / "important.txt"
        evil_file.write_text("do not delete")

        _seed_install(
            plugin._uow,
            99,
            file_path=str(evil_file),
            rom_dir=str(evil_dir),
        )

        await plugin.remove_rom(99)
        # The evil dir/file should NOT be deleted
        assert evil_dir.exists()
        assert evil_file.exists()
        # The install record is still cleaned up (the rejection is silent)
        assert plugin._uow.rom_installs.get(99) is None

    @pytest.mark.asyncio
    async def test_rejects_file_path_outside_roms_base(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        evil_file = tmp_path / "etc" / "passwd"
        evil_file.parent.mkdir(parents=True)
        evil_file.write_text("root:x:0:0")

        _seed_install(
            plugin._uow,
            99,
            file_path=str(evil_file),
            rom_dir=None,
        )

        await plugin.remove_rom(99)
        assert evil_file.exists()
        assert plugin._uow.rom_installs.get(99) is None


class TestPathTraversalFsName:
    """Tests for path traversal safety in download — fs_name sanitization."""

    @pytest.mark.asyncio
    async def test_fs_name_traversal_sanitized(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_detail = {
            "id": 77,
            "name": "Evil ROM",
            "fs_name": "../../../etc/passwd",
            "fs_size_bytes": 1024,
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)

        def _close_coro_task(coro):
            coro.close()
            return MagicMock()

        plugin._download_service._loop.create_task = _close_coro_task

        plugin._download_service._download_file_store.disk_free = lambda _path: 500 * 1024 * 1024
        result = await plugin.start_download(77)

        assert result["success"] is True
        # The target path should use sanitized basename only
        queue_entry = plugin._download_service._download_queue[77]
        assert queue_entry["file_name"] == "passwd"
        # The coroutine was created — just verify the queue entry is safe
        assert ".." not in queue_entry["file_name"]


class TestCleanupPartialDownload:
    """Tests for _cleanup_partial_download — all paths."""

    def test_cleans_tmp_file_single(self, plugin, tmp_path):
        target = str(tmp_path / "game.z64")
        tmp_file = tmp_path / "game.z64.tmp"
        tmp_file.write_text("partial")

        plugin._download_service._cleanup_partial_download(target, False, "game.z64")
        assert not tmp_file.exists()

    def test_cleans_zip_tmp_multi(self, plugin, tmp_path):
        target = str(tmp_path / "game.zip")
        zip_tmp = tmp_path / "game.zip.zip.tmp"
        zip_tmp.write_text("partial zip")

        plugin._download_service._cleanup_partial_download(target, True, "game.zip")
        assert not zip_tmp.exists()

    def test_cleans_extract_dir(self, plugin, tmp_path):
        target = str(tmp_path / "game.zip")
        extract_dir = tmp_path / "game"
        extract_dir.mkdir()
        (extract_dir / "disc1.bin").write_bytes(b"\x00" * 100)

        plugin._download_service._cleanup_partial_download(target, True, "game.zip")
        assert not extract_dir.exists()

    def test_cleanup_errors_are_caught(self, plugin, tmp_path):
        """Cleanup should not raise even if files don't exist."""
        target = str(tmp_path / "nonexistent.z64")
        # Should not raise
        plugin._download_service._cleanup_partial_download(target, False, "nonexistent.z64")
        plugin._download_service._cleanup_partial_download(target, True, "nonexistent.zip")


class TestDoDownloadCancelled:
    """Tests for _do_download — cancelled mid-download."""

    @pytest.mark.asyncio
    async def test_cancelled_sets_status_and_cleans_up(self, plugin, tmp_path):
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "zelda.z64")

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
            "has_multiple_files": False,
        }

        def fake_download_cancel(_rom_id, _filename, dest, _progress_callback=None):
            raise asyncio.CancelledError()

        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[42] = {"rom_id": 42, "status": "downloading", "progress": 0}

        with (
            patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download_cancel),
            pytest.raises(asyncio.CancelledError),
        ):
            await plugin._download_service._do_download(42, rom_detail, target_path, "n64", "zelda.z64")

        assert plugin._download_service._download_queue[42]["status"] == "cancelled"
        assert not os.path.exists(target_path)
        assert plugin._uow.rom_installs.get(42) is None


class TestDoDownloadZipFailure:
    """Tests for _do_download — ZIP extraction failure."""

    @pytest.mark.asyncio
    async def test_zip_failure_sets_failed_and_cleans_up(self, plugin, tmp_path):
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "game.zip")

        rom_detail = {
            "id": 66,
            "name": "Bad ZIP Game",
            "fs_name": "game.zip",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": True,
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            # Write invalid data (not a real zip)
            with open(dest, "wb") as f:
                f.write(b"not a zip file")

        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[66] = {"rom_id": 66, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(66, rom_detail, target_path, "psx", "game.zip")

        assert plugin._download_service._download_queue[66]["status"] == "failed"
        # .zip.tmp should be cleaned up
        assert not os.path.exists(target_path + ".zip.tmp")


class TestDoDownloadFailureEmit:
    """Tests for _do_download — ``download_failed`` event emission."""

    @pytest.mark.asyncio
    async def test_failure_emits_download_failed(self, plugin, tmp_path):
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "zelda.z64")

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
            "has_multiple_files": False,
        }

        def fake_download(_rom_id, _filename, _dest, _progress_callback=None):
            raise OSError("simulated network drop")

        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[42] = {"rom_id": 42, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(42, rom_detail, target_path, "n64", "zelda.z64")

        # download_failed event emitted with the expected payload shape
        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "download_failed"]
        assert len(emit_calls) == 1
        payload = emit_calls[0][0][1]
        assert payload["rom_id"] == 42
        assert payload["rom_name"] == "Zelda"
        assert payload["platform_name"] == "Nintendo 64"
        assert payload["error_message"] == "simulated network drop"
        # No download_complete in the failure path
        assert not [c for c in decky.emit.call_args_list if c[0][0] == "download_complete"]
        # Queue status reflects the failure
        assert plugin._download_service._download_queue[42]["status"] == "failed"
        assert plugin._download_service._download_queue[42]["error"] == "simulated network drop"


class TestDoDownloadInvariantFailure:
    """Tests for _do_download — RomInstall invariant rejects the ROM data.

    A non-positive ``rom_id`` fails ``RomInstall.mark_installed``. The worker
    catches the ValueError, removes the just-installed artifact, persists no
    record, and the download is reported as failed — no exception escapes.
    """

    @pytest.mark.asyncio
    async def test_single_file_invariant_failure_cleans_up_and_persists_nothing(self, plugin, tmp_path):
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "zelda.z64")

        rom_detail = {
            "id": 0,
            "name": "Bad ROM",
            "fs_name": "zelda.z64",
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
            "has_multiple_files": False,
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(b"\x00" * 64)

        plugin._download_service._loop = asyncio.get_event_loop()
        # rom_id=0 violates RomInstall's invariant (rom_id must be positive).
        plugin._download_service._download_queue[0] = {"rom_id": 0, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(0, rom_detail, target_path, "n64", "zelda.z64")

        # Download reported as failed via the canonical failure path.
        assert plugin._download_service._download_queue[0]["status"] == "failed"
        assert "Invalid install metadata" in plugin._download_service._download_queue[0]["error"]
        # The just-renamed file was cleaned up — nothing left dangling.
        assert not os.path.exists(target_path)
        # No RomInstall record persisted.
        assert plugin._uow.rom_installs.get(0) is None
        assert list(plugin._uow.rom_installs.iter_all()) == []
        # download_failed emitted, no download_complete.
        assert [c for c in decky.emit.call_args_list if c[0][0] == "download_failed"]
        assert not [c for c in decky.emit.call_args_list if c[0][0] == "download_complete"]

    @pytest.mark.asyncio
    async def test_multi_file_invariant_failure_removes_extract_dir(self, plugin, tmp_path):
        import zipfile as zf
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "FF7.zip")

        zip_content_path = tmp_path / "source.zip"
        with zf.ZipFile(str(zip_content_path), "w") as z:
            z.writestr("disc1.cue", "FILE disc1.bin BINARY")
            z.writestr("disc1.bin", b"\x00" * 100)
        zip_bytes = zip_content_path.read_bytes()

        rom_detail = {
            "id": 0,
            "name": "Bad Multi ROM",
            "fs_name": "FF7.zip",
            "fs_name_no_ext": "FF7",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": True,
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(zip_bytes)

        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[0] = {"rom_id": 0, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(0, rom_detail, target_path, "psx", "FF7.zip")

        assert plugin._download_service._download_queue[0]["status"] == "failed"
        assert "Invalid install metadata" in plugin._download_service._download_queue[0]["error"]
        # The extracted directory was removed by the invariant-failure cleanup.
        assert not (roms_dir / "FF7").exists()
        # No RomInstall record persisted.
        assert plugin._uow.rom_installs.get(0) is None
        assert list(plugin._uow.rom_installs.iter_all()) == []


class TestStartDownloadReDownload:
    """Test start_download allows re-download after completion."""

    @pytest.mark.asyncio
    async def test_re_download_after_completed(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "fs_size_bytes": 1024,
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)

        def _close_coro_task(coro):
            coro.close()
            return MagicMock()

        plugin._download_service._loop.create_task = _close_coro_task

        # Set status to completed (previous download)
        plugin._download_service._download_queue[42] = {"status": "completed"}

        plugin._download_service._download_file_store.disk_free = lambda _path: 500 * 1024 * 1024
        result = await plugin.start_download(42)

        assert result["success"] is True
        assert plugin._download_service._download_queue[42]["status"] == "downloading"


class TestMaybeGenerateM3uMixedFormats:
    """Test M3U generation with mixed disc formats."""

    def test_mixed_cue_and_chd(self, plugin, tmp_path):
        (tmp_path / "disc1.cue").write_text("cue 1")
        (tmp_path / "disc2.chd").write_bytes(b"\x00" * 100)

        rom_detail = {"fs_name_no_ext": "Mixed Game", "name": "Mixed Game"}
        plugin._download_service._maybe_generate_m3u_io(str(tmp_path), rom_detail)

        m3u_path = tmp_path / "Mixed Game.m3u"
        assert m3u_path.exists()
        content = m3u_path.read_text().strip()
        lines = content.split("\n")
        assert len(lines) == 2
        # Should include both formats
        exts = {os.path.splitext(line)[1] for line in lines}
        assert ".cue" in exts
        assert ".chd" in exts


class TestMaybeGenerateM3uSpecialCharacters:
    """Test M3U preserves special characters in filenames."""

    def test_special_characters_preserved(self, plugin, tmp_path):
        names = [
            "Game (Disc 1) [Japan].cue",
            "Game (Disc 2) [Japan].cue",
        ]
        for name in names:
            (tmp_path / name).write_text("cue data")

        rom_detail = {"fs_name_no_ext": "Game", "name": "Game"}
        plugin._download_service._maybe_generate_m3u_io(str(tmp_path), rom_detail)

        m3u_path = tmp_path / "Game.m3u"
        assert m3u_path.exists()
        content = m3u_path.read_text().strip()
        lines = content.split("\n")
        assert len(lines) == 2
        # Verify special chars preserved exactly
        for name in names:
            assert name in lines


class TestUninstallAllRomsMixedResults:
    """Test uninstall_all_roms with mixed success/failure."""

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        # Create a real file that can be deleted
        roms_dir = tmp_path / "retrodeck" / "roms" / "n64"
        roms_dir.mkdir(parents=True)
        good_file = roms_dir / "game_a.z64"
        good_file.write_text("data")

        # Create another file but make deletion fail by using a non-safe path
        # (outside roms dir, which _delete_rom_files should reject silently)
        bad_file = tmp_path / "outside" / "game_b.z64"
        bad_file.parent.mkdir(parents=True)
        bad_file.write_text("data")

        _seed_install(plugin._uow, 1, file_path=str(good_file), rom_dir=None)
        _seed_install(plugin._uow, 2, file_path=str(bad_file), rom_dir=None, system="snes")

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        # good_file should be deleted
        assert not good_file.exists()
        # bad_file should still exist (outside roms dir)
        assert bad_file.exists()
        # The path rejection is silent (no exception), so both records are cleared.
        assert result["removed_count"] == 2
        assert list(plugin._uow.rom_installs.iter_all()) == []


class TestRemoveRomFileAlreadyGone:
    """Test remove_rom when file is already deleted."""

    @pytest.mark.asyncio
    async def test_file_already_gone_cleans_state(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        # Install record exists but the file is gone on disk.
        _seed_install(
            plugin._uow,
            42,
            file_path=str(tmp_path / "retrodeck" / "roms" / "n64" / "gone.z64"),
            rom_dir=None,
        )

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        assert plugin._uow.rom_installs.get(42) is None


class TestUrlEncodedFilenameRename:
    """Tests for URL-encoded filename fix after ZIP extraction."""

    @pytest.mark.asyncio
    async def test_renames_url_encoded_files_after_extract(self, plugin, tmp_path):
        import zipfile as zf
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "Vagrant Story (USA).zip")

        # Create a ZIP with URL-encoded filenames (as RomM generates)
        zip_content_path = tmp_path / "source.zip"
        with zf.ZipFile(str(zip_content_path), "w") as z:
            z.writestr("Vagrant%20Story%20%28USA%29.m3u", "Vagrant%20Story%20%28USA%29%20%28Disc%201%29.chd\n")
            z.writestr("Vagrant%20Story%20%28USA%29%20%28Disc%201%29.chd", b"\x00" * 100)
        zip_bytes = zip_content_path.read_bytes()

        rom_detail = {
            "id": 99,
            "name": "Vagrant Story (USA)",
            "fs_name": "Vagrant Story (USA).zip",
            "fs_name_no_ext": "Vagrant Story (USA)",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": True,
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(zip_bytes)

        _seed_rom(plugin._uow, 99, platform_slug="psx")
        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[99] = {"rom_id": 99, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(99, rom_detail, target_path, "psx", "Vagrant Story (USA).zip")

        extract_dir = roms_dir / "Vagrant Story (USA)"
        # URL-encoded filenames should be decoded
        assert (extract_dir / "Vagrant Story (USA).m3u").exists()
        assert (extract_dir / "Vagrant Story (USA) (Disc 1).chd").exists()
        # The percent-encoded versions should NOT exist
        assert not (extract_dir / "Vagrant%20Story%20%28USA%29.m3u").exists()
        assert not (extract_dir / "Vagrant%20Story%20%28USA%29%20%28Disc%201%29.chd").exists()

    @pytest.mark.asyncio
    async def test_leaves_normal_filenames_alone(self, plugin, tmp_path):
        import zipfile as zf
        from unittest.mock import patch

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )
        decky.emit.reset_mock()

        roms_dir = tmp_path / "retrodeck" / "roms" / "psx"
        roms_dir.mkdir(parents=True)
        target_path = str(roms_dir / "FF7.zip")

        zip_content_path = tmp_path / "source.zip"
        with zf.ZipFile(str(zip_content_path), "w") as z:
            z.writestr("disc1.cue", "FILE disc1.bin BINARY")
            z.writestr("disc1.bin", b"\x00" * 100)
            z.writestr("disc2.cue", "FILE disc2.bin BINARY")
            z.writestr("disc2.bin", b"\x00" * 100)
        zip_bytes = zip_content_path.read_bytes()

        rom_detail = {
            "id": 55,
            "name": "Final Fantasy VII",
            "fs_name": "FF7.zip",
            "fs_name_no_ext": "FF7",
            "platform_slug": "psx",
            "platform_name": "PlayStation",
            "has_multiple_files": True,
        }

        def fake_download(_rom_id, _filename, dest, _progress_callback=None):
            with open(dest, "wb") as f:
                f.write(zip_bytes)

        _seed_rom(plugin._uow, 55, platform_slug="psx")
        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[55] = {"rom_id": 55, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(55, rom_detail, target_path, "psx", "FF7.zip")

        extract_dir = roms_dir / "FF7"
        # Normal filenames should be unchanged
        assert (extract_dir / "disc1.cue").exists()
        assert (extract_dir / "disc1.bin").exists()
        assert (extract_dir / "disc2.cue").exists()
        assert (extract_dir / "disc2.bin").exists()


class TestCleanupLeftoverTmpFiles:
    def test_removes_tmp_file(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        system_dir = tmp_path / "retrodeck" / "roms" / "n64"
        system_dir.mkdir(parents=True)
        tmp_file = system_dir / "zelda.z64.tmp"
        tmp_file.write_text("partial download")

        plugin._download_service.cleanup_leftover_tmp_files()
        assert not tmp_file.exists()

    def test_removes_zip_tmp_file(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        system_dir = tmp_path / "retrodeck" / "roms" / "psx"
        system_dir.mkdir(parents=True)
        tmp_file = system_dir / "game.zip.tmp"
        tmp_file.write_text("partial zip")

        plugin._download_service.cleanup_leftover_tmp_files()
        assert not tmp_file.exists()

    def test_keeps_real_rom_files(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        system_dir = tmp_path / "retrodeck" / "roms" / "n64"
        system_dir.mkdir(parents=True)
        real_rom = system_dir / "zelda.z64"
        real_rom.write_text("real rom")
        bin_file = system_dir / "game.bin"
        bin_file.write_text("real bin")
        cue_file = system_dir / "game.cue"
        cue_file.write_text("real cue")

        plugin._download_service.cleanup_leftover_tmp_files()
        assert real_rom.exists()
        assert bin_file.exists()
        assert cue_file.exists()

    def test_removes_bios_tmp(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        bios_dir = tmp_path / "retrodeck" / "bios" / "dc"
        bios_dir.mkdir(parents=True)
        tmp_file = bios_dir / "dc_boot.bin.tmp"
        tmp_file.write_text("partial bios")

        plugin._download_service.cleanup_leftover_tmp_files()
        assert not tmp_file.exists()

    def test_no_roms_dir_no_crash(self, plugin, tmp_path):
        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )
        # No retrodeck/roms directory exists — should not crash
        plugin._download_service.cleanup_leftover_tmp_files()

    def test_handles_permission_error(self, plugin, tmp_path, caplog):
        import logging

        import decky
        from fakes.fake_download_file_store import FakeDownloadFileStore

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        # Stage a virtual tmp file via the fake adapter so the service can
        # discover it via walk_files_matching_suffixes; the fake's
        # ``remove_failures`` set makes the subsequent remove raise OSError.
        roms_base = str(tmp_path / "retrodeck" / "roms")
        bios_base = str(tmp_path / "retrodeck" / "bios")
        tmp_file_path = os.path.join(roms_base, "n64", "zelda.z64.tmp")

        fake = FakeDownloadFileStore()
        fake.make_dirs(roms_base)
        fake.make_dirs(bios_base)
        fake.files[tmp_file_path] = b"partial"
        fake.remove_failures.add(tmp_file_path)
        plugin._download_service._download_file_store = fake

        with caplog.at_level(logging.WARNING, logger="test_romm"):
            plugin._download_service.cleanup_leftover_tmp_files()

        # Per-file warning must be emitted; sister-PR pattern in
        # SteamGridService.prune_orphaned_artwork_cache.
        assert any(
            "Failed to remove tmp file" in rec.message and tmp_file_path in rec.message for rec in caplog.records
        ), f"expected warning about {tmp_file_path}, got {[r.message for r in caplog.records]}"
        # File still present in fake — service swallowed the OSError.
        assert tmp_file_path in fake.files


class TestPruneDownloadQueue:
    def test_keeps_active_downloads(self, plugin):
        """Active (downloading) items are never pruned."""
        for i in range(60):
            plugin._download_service._download_queue[i] = {"rom_id": i, "status": "downloading"}
        plugin._download_service._prune_download_queue()
        assert len(plugin._download_service._download_queue) == 60

    def test_removes_oldest_terminal_when_over_limit(self, plugin):
        """When there are more than 50 terminal items, remove the oldest."""
        # Insert 60 completed items (rom_id 0..59)
        for i in range(60):
            plugin._download_service._download_queue[i] = {"rom_id": i, "status": "completed"}
        plugin._download_service._prune_download_queue()
        # Should keep the 50 most recent (10..59)
        assert len(plugin._download_service._download_queue) == 50
        for i in range(10):
            assert i not in plugin._download_service._download_queue
        for i in range(10, 60):
            assert i in plugin._download_service._download_queue

    def test_does_nothing_when_under_limit(self, plugin):
        """No pruning if terminal count is at or below the limit."""
        for i in range(30):
            plugin._download_service._download_queue[i] = {"rom_id": i, "status": "completed"}
        plugin._download_service._prune_download_queue()
        assert len(plugin._download_service._download_queue) == 30

    def test_does_nothing_at_exactly_limit(self, plugin):
        """No pruning when terminal count is exactly 50."""
        for i in range(50):
            plugin._download_service._download_queue[i] = {"rom_id": i, "status": "failed"}
        plugin._download_service._prune_download_queue()
        assert len(plugin._download_service._download_queue) == 50

    def test_mixed_active_and_terminal(self, plugin):
        """Active items are kept; only terminal items count toward the limit."""
        # 5 active + 55 completed = 55 terminal -> prune 5 oldest terminal
        for i in range(5):
            plugin._download_service._download_queue[1000 + i] = {"rom_id": 1000 + i, "status": "downloading"}
        for i in range(55):
            plugin._download_service._download_queue[i] = {"rom_id": i, "status": "completed"}
        plugin._download_service._prune_download_queue()
        # 5 active + 50 terminal = 55 total
        assert len(plugin._download_service._download_queue) == 55
        # All active still present
        for i in range(5):
            assert 1000 + i in plugin._download_service._download_queue
        # Oldest 5 terminal removed (0..4)
        for i in range(5):
            assert i not in plugin._download_service._download_queue
        # Remaining terminal still present (5..54)
        for i in range(5, 55):
            assert i in plugin._download_service._download_queue

    def test_handles_all_terminal_statuses(self, plugin):
        """Completed, failed, and cancelled items are all treated as terminal."""
        for i in range(20):
            plugin._download_service._download_queue[i] = {"rom_id": i, "status": "completed"}
        for i in range(20, 40):
            plugin._download_service._download_queue[i] = {"rom_id": i, "status": "failed"}
        for i in range(40, 60):
            plugin._download_service._download_queue[i] = {"rom_id": i, "status": "cancelled"}
        plugin._download_service._prune_download_queue()
        assert len(plugin._download_service._download_queue) == 50
        # Oldest 10 (all completed, 0..9) should be removed
        for i in range(10):
            assert i not in plugin._download_service._download_queue


class TestStartDownloadCreateTaskFailure:
    """Tests for start_download when create_task raises."""

    @pytest.mark.asyncio
    async def test_create_task_failure_returns_error(self, plugin, tmp_path):
        from unittest.mock import AsyncMock

        import decky

        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        rom_detail = {
            "id": 42,
            "name": "Zelda",
            "fs_name": "zelda.z64",
            "fs_size_bytes": 1024,
            "platform_slug": "n64",
            "platform_name": "Nintendo 64",
        }

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.run_in_executor = AsyncMock(return_value=rom_detail)
        plugin._download_service._loop.create_task = MagicMock(side_effect=RuntimeError("loop closed"))

        plugin._download_service._download_file_store.disk_free = lambda _path: 500 * 1024 * 1024
        result = await plugin.start_download(42)

        assert result["success"] is False
        assert "Failed to start download" in result["message"]
        # Should not remain in download_in_progress
        assert 42 not in plugin._download_service._download_in_progress


class TestShutdown:
    """Tests for DownloadService.shutdown — cancel active tasks + clear tracking."""

    @pytest.mark.asyncio
    async def test_shutdown_cancels_active_tasks_and_clears(self, plugin):
        task_a = MagicMock()
        task_b = MagicMock()
        plugin._download_service._download_tasks[1] = task_a
        plugin._download_service._download_tasks[2] = task_b

        await plugin._download_service.shutdown()

        task_a.cancel.assert_called_once_with()
        task_b.cancel.assert_called_once_with()
        assert plugin._download_service._download_tasks == {}

    @pytest.mark.asyncio
    async def test_shutdown_no_tasks_is_noop(self, plugin):
        # No tasks registered — must not raise.
        await plugin._download_service.shutdown()
        assert plugin._download_service._download_tasks == {}


class TestCleanupLeftoverTmpFilesNoRetrodeckPaths:
    """Tests for cleanup_leftover_tmp_files when retrodeck paths resolve to empty.

    Covers the early-return guard inside _clean_rom_tmp_files /
    _clean_bios_tmp_files when retrodeck.json is absent (roms_path()
    / bios_path() return ""). Service must not walk an empty path.
    """

    def test_empty_roms_and_bios_paths_skip_walk(self, plugin):
        from fakes.fake_download_file_store import FakeDownloadFileStore

        fake = FakeDownloadFileStore()
        plugin._download_service._download_file_store = fake
        # retrodeck_paths present but both helpers return empty (no
        # retrodeck.json) — service must early-return on each branch.
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(roms="", bios="")

        plugin._download_service.cleanup_leftover_tmp_files()

        assert fake.walk_calls == []


class TestMakeProgressCallback:
    """Tests for _make_progress_callback — throttling, logging, emission."""

    def test_progress_callback_updates_queue_and_dispatches_emit(self, plugin):
        # Pre-populate the queue entry the callback updates in place.
        plugin._download_service._download_queue[7] = {
            "rom_id": 7,
            "rom_name": "Mario",
            "platform_name": "N64",
            "file_name": "mario.z64",
            "status": "downloading",
            "progress": 0,
            "bytes_downloaded": 0,
            "total_bytes": 0,
        }
        fake_clock = FakeClock()
        plugin._download_service._clock = fake_clock

        # Replace the event loop with a MagicMock so call_soon_threadsafe
        # is observable without actually scheduling on the real loop.
        # Run create_task eagerly inside call_soon_threadsafe so the
        # coroutine returned by emit() gets consumed (otherwise it
        # leaks as un-awaited).
        scheduled_calls: list[int] = []

        def _eager_call_soon_threadsafe(fn, *args, **kwargs):
            scheduled_calls.append(1)
            return fn(*args, **kwargs)

        plugin._download_service._loop = MagicMock()
        plugin._download_service._loop.call_soon_threadsafe = _eager_call_soon_threadsafe
        plugin._download_service._loop.create_task = lambda coro: coro.close() or MagicMock()
        emit_calls = []

        def _record_emit(event, payload):
            emit_calls.append((event, payload))

            # Return a coroutine because the real emit is awaitable —
            # the closure passes it to create_task.
            async def _noop():
                return None

            return _noop()

        plugin._download_service._emit = _record_emit

        cb = plugin._download_service._make_progress_callback(7, "Mario", "N64", "mario.z64")
        # Advance the clock so both branches fire (log-throttle >= 30s
        # AND emit-throttle >= 0.5s — both gated on now - last_<x>).
        fake_clock.advance(60)

        cb(512, 1024)

        # Queue entry must have been updated in place.
        entry = plugin._download_service._download_queue[7]
        assert entry["progress"] == 0.5
        assert entry["bytes_downloaded"] == 512
        assert entry["total_bytes"] == 1024

        # call_soon_threadsafe must have been invoked once to schedule
        # the emit coroutine.
        assert len(scheduled_calls) == 1
        # Emit was called with the right event name + payload shape.
        assert len(emit_calls) == 1
        event, payload = emit_calls[0]
        assert event == "download_progress"
        assert payload["rom_id"] == 7
        assert payload["progress"] == 0.5
        assert payload["bytes_downloaded"] == 512
        assert payload["total_bytes"] == 1024

    def test_progress_callback_throttles_intermediate_emits(self, plugin):
        plugin._download_service._download_queue[8] = {
            "rom_id": 8,
            "status": "downloading",
            "progress": 0,
            "bytes_downloaded": 0,
            "total_bytes": 0,
        }
        fake_clock = FakeClock()
        plugin._download_service._clock = fake_clock
        plugin._download_service._loop = MagicMock()
        plugin._download_service._emit = MagicMock(return_value=None)

        cb = plugin._download_service._make_progress_callback(8, "Game", "Plat", "game.bin")

        # First call: monotonic == 0; last_emit starts at 0.0 too, but
        # downloaded < total so the throttle check (now - last_emit <
        # 0.5 AND downloaded < total) returns early. No update.
        cb(100, 1000)
        assert plugin._download_service._download_queue[8]["bytes_downloaded"] == 0
        assert plugin._download_service._loop.call_soon_threadsafe.call_count == 0

        # Final call: downloaded == total bypasses the throttle even
        # when no time elapsed — the closure always emits the final
        # completion frame.
        cb(1000, 1000)
        assert plugin._download_service._download_queue[8]["bytes_downloaded"] == 1000
        assert plugin._download_service._loop.call_soon_threadsafe.call_count == 1

    def test_progress_callback_handles_zero_total(self, plugin):
        """total == 0 must not divide-by-zero — pct/progress fall back to 0."""
        plugin._download_service._download_queue[9] = {
            "rom_id": 9,
            "status": "downloading",
            "progress": 0,
            "bytes_downloaded": 0,
            "total_bytes": 0,
        }
        fake_clock = FakeClock()
        plugin._download_service._clock = fake_clock
        plugin._download_service._loop = MagicMock()
        plugin._download_service._emit = MagicMock(return_value=None)

        cb = plugin._download_service._make_progress_callback(9, "Game", "Plat", "game.bin")
        # Advance past both throttles so the log + emit branches both
        # execute with total == 0 — exercises the zero-guard branches.
        fake_clock.advance(60)
        cb(0, 0)

        entry = plugin._download_service._download_queue[9]
        assert entry["progress"] == 0
        assert entry["total_bytes"] == 0
        # Emit was still scheduled — final-frame path triggers when
        # downloaded >= total (both zero satisfies the >= check).
        assert plugin._download_service._loop.call_soon_threadsafe.call_count == 1


class TestCleanupPartialDownloadFailureInjection:
    """Tests for _cleanup_partial_download — adapter raises mid-cleanup.

    The cleanup loop must swallow per-path OSError so one failing
    remove never blocks the others, AND the multi-file remove_tree
    branch must swallow its own failure the same way (logged as a
    warning, no re-raise).
    """

    def test_remove_failures_are_logged_and_other_paths_still_removed(self, plugin, caplog):
        import logging

        from fakes.fake_download_file_store import FakeDownloadFileStore

        fake = FakeDownloadFileStore()
        target = "/roms/n64/game.z64"
        # Stage all three candidate paths so each remove call has
        # something to act on; mark the .tmp variant as failing.
        fake.files[target + _ZIP_TMP_EXT_LITERAL] = b"junk1"
        fake.files[target + _TMP_EXT_LITERAL] = b"junk2"
        fake.files[target] = b"junk3"
        fake.remove_failures.add(target + _TMP_EXT_LITERAL)
        plugin._download_service._download_file_store = fake

        with caplog.at_level(logging.WARNING, logger="test_romm"):
            plugin._download_service._cleanup_partial_download(target, False, "game.z64")

        # The failing path is still in the fake (remove raised); the
        # other two were successfully removed.
        assert (target + _TMP_EXT_LITERAL) in fake.files
        assert (target + _ZIP_TMP_EXT_LITERAL) not in fake.files
        assert target not in fake.files
        # Warning mentions the failing path.
        assert any(
            "Cleanup failed for" in rec.message and (target + _TMP_EXT_LITERAL) in rec.message for rec in caplog.records
        )

    def test_remove_tree_failure_is_logged_and_swallowed(self, plugin, caplog):
        import logging

        from fakes.fake_download_file_store import FakeDownloadFileStore

        fake = FakeDownloadFileStore()
        target = "/roms/psx/game.zip"
        extract_dir = "/roms/psx/game"
        fake.make_dirs(extract_dir)
        fake.files[os.path.join(extract_dir, "disc1.bin")] = b"\x00" * 16
        # Inject a remove_tree failure for the extract dir; remove on
        # the three tmp paths is a no-op (paths absent).
        fake.remove_tree_failures.add(extract_dir)
        plugin._download_service._download_file_store = fake

        with caplog.at_level(logging.WARNING, logger="test_romm"):
            # Must NOT raise even though remove_tree raises.
            plugin._download_service._cleanup_partial_download(target, True, "game.zip")

        # The dir is still present (remove_tree raised before clearing).
        assert extract_dir in fake.dirs
        # Warning mentions the failing directory.
        assert any(
            "Cleanup failed for directory" in rec.message and extract_dir in rec.message for rec in caplog.records
        )


# Internal constants — re-declared so the test file doesn't reach into
# the service module's private names. Keep in sync with services/downloads.py.
_ZIP_TMP_EXT_LITERAL = ".zip.tmp"
_TMP_EXT_LITERAL = ".tmp"
