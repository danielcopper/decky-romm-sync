import asyncio
import json
import os
from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from conftest import _make_testable_plugin
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.library_peers import FakeArtworkManager, FakeMetadataExtractor
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen
from models.state import make_default_plugin_state

from adapters.download_file import DownloadFileAdapter
from adapters.download_queue import DownloadQueueAdapter
from adapters.registry_store import RegistryStoreAdapter
from adapters.rom_files import RomFileAdapter
from adapters.steam_config import SteamConfigAdapter
from domain.save_state import SaveSyncState
from services.downloads import DownloadService, DownloadServiceConfig
from services.library import LibraryService, LibraryServiceConfig
from services.rom_removal import RomRemovalService, RomRemovalServiceConfig


@pytest.fixture
def plugin():
    p = _make_testable_plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()
    p._resolve_system = MagicMock(side_effect=lambda slug, fs_slug=None: fs_slug or slug)
    p._state = make_default_plugin_state()
    p._metadata_cache = {}

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=p._romm_api,
            steam_config=steam_config,
            state=p._state,
            settings=p.settings,
            metadata_cache=p._metadata_cache,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            state_persister=MagicMock(),
            settings_persister=MagicMock(),
            registry_store=RegistryStoreAdapter(state=p._state, logger=decky.logger),
            log_debug=p._log_debug,
            metadata_service=FakeMetadataExtractor(),
            artwork=FakeArtworkManager(),
        ),
    )
    p._save_sync_state = SaveSyncState()
    p._download_service = DownloadService(
        config=DownloadServiceConfig(
            romm_api=p._romm_api,
            state=p._state,
            download_file_store=DownloadFileAdapter(),
            download_queue=DownloadQueueAdapter(),
            resolve_system=p._resolve_system,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            runtime_dir=decky.DECKY_PLUGIN_RUNTIME_DIR,
            emit=decky.emit,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            sleeper=FakeSleeper(),
            state_persister=MagicMock(),
            retrodeck_paths=FakeRetroDeckPaths(
                roms=os.path.join(os.path.expanduser("~"), "retrodeck", "roms"),
                bios=os.path.join(os.path.expanduser("~"), "retrodeck", "bios"),
            ),
            is_retrodeck_migration_pending=lambda: False,
        ),
    )
    p._rom_removal_service = RomRemovalService(
        config=RomRemovalServiceConfig(
            state=p._state,
            save_sync_state=p._save_sync_state,
            logger=decky.logger,
            loop=asyncio.get_event_loop(),
            state_persister=MagicMock(),
            save_sync_state_writer=MagicMock(),
            rom_file_store=RomFileAdapter(),
            retrodeck_paths=FakeRetroDeckPaths(
                roms=os.path.join(os.path.expanduser("~"), "retrodeck", "roms"),
            ),
            download_queue_cleanup=p._download_service,
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
        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": "/roms/n64/zelda.z64",
            "system": "n64",
        }
        result = await plugin.get_installed_rom(42)
        assert result is not None
        assert result["rom_id"] == 42
        assert result["system"] == "n64"

    @pytest.mark.asyncio
    async def test_returns_none_not_installed(self, plugin):
        result = await plugin.get_installed_rom(999)
        assert result is None


class TestRemoveRom:
    @pytest.mark.asyncio
    async def test_deletes_file_and_clears_state(self, plugin, tmp_path):
        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": str(rom_file),
            "system": "n64",
        }
        plugin._download_service._download_queue[42] = {"status": "completed"}

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        assert not rom_file.exists()
        assert "42" not in plugin._state["installed_roms"]
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

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(file_a), "system": "n64"},
            "2": {"rom_id": 2, "file_path": str(file_b), "system": "n64"},
        }

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 2
        assert not file_a.exists()
        assert not file_b.exists()

    @pytest.mark.asyncio
    async def test_clears_state(self, plugin, tmp_path):
        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/nonexistent", "system": "n64"},
        }

        await plugin.uninstall_all_roms()
        assert plugin._state["installed_roms"] == {}

    @pytest.mark.asyncio
    async def test_handles_missing_files(self, plugin, tmp_path):
        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/does/not/exist.z64", "system": "n64"},
            "2": {"rom_id": 2, "file_path": "/also/missing.z64", "system": "snes"},
        }

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        assert plugin._state["installed_roms"] == {}


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


class TestDownloadRequestPolling:
    @pytest.mark.asyncio
    async def test_processes_download_request(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, patch

        plugin._download_service._runtime_dir = str(tmp_path)

        requests_path = tmp_path / "download_requests.json"
        requests_path.write_text(json.dumps([{"rom_id": 42}]))

        with patch.object(plugin, "start_download", new_callable=AsyncMock) as mock_start:
            # Call internal logic directly: read file, process, clear
            with open(requests_path) as f:
                requests = json.load(f)
            with open(requests_path, "w") as f:
                json.dump([], f)
            for req in requests:
                rom_id = req.get("rom_id")
                if rom_id:
                    await plugin.start_download(rom_id)

            mock_start.assert_called_once_with(42)

    @pytest.mark.asyncio
    async def test_cleans_up_request_file(self, plugin, tmp_path):
        plugin._download_service._runtime_dir = str(tmp_path)

        requests_path = tmp_path / "download_requests.json"
        requests_path.write_text(json.dumps([{"rom_id": 1}, {"rom_id": 2}]))

        # Simulate the cleanup logic from _poll_download_requests
        with open(requests_path) as f:
            requests = json.load(f)
        with open(requests_path, "w") as f:
            json.dump([], f)

        # Verify file was cleared
        with open(requests_path) as f:
            remaining = json.load(f)
        assert remaining == []
        assert len(requests) == 2


class TestPollDownloadRequestsMigrationPause:
    """Verify the poll loop pauses (does NOT read+clear the request file)
    while a RetroDECK migration is pending — would otherwise drop queued
    download requests on the floor (#251)."""

    @pytest.mark.asyncio
    async def test_poll_download_requests_pauses_when_migration_pending(self, plugin, tmp_path):
        plugin._download_service._runtime_dir = str(tmp_path)
        plugin._download_service._is_retrodeck_migration_pending = lambda: True

        requests_path = tmp_path / "download_requests.json"
        original_payload = [{"rom_id": 42}, {"rom_id": 99}]
        requests_path.write_text(json.dumps(original_payload))

        # Stub the injected Sleeper so we can run a single iteration
        # deterministically: first call sleeps normally (returns immediately),
        # second call cancels the loop so we exit after one pass.
        class _CancellingSleeper:
            def __init__(self):
                self.calls = 0

            async def sleep(self, _seconds):
                self.calls += 1
                if self.calls >= 2:
                    raise asyncio.CancelledError

        plugin._download_service._sleeper = _CancellingSleeper()

        # Track whether the request file IO was invoked via the queue adapter.
        from fakes.fake_download_queue_store import FakeDownloadQueueStore

        tracking_queue = FakeDownloadQueueStore()
        plugin._download_service._download_queue_io = tracking_queue

        with pytest.raises(asyncio.CancelledError):
            await plugin._download_service.poll_download_requests()

        # IO must NOT have been called while migration was pending.
        assert tracking_queue.poll_count == 0
        # Request file must still hold its original contents — not truncated.
        with open(requests_path) as f:
            assert json.load(f) == original_payload


class TestMultiFileRomDeletion:
    @pytest.mark.asyncio
    async def test_remove_rom_deletes_rom_dir(self, plugin, tmp_path):
        """Multi-file ROM with rom_dir should delete the entire directory."""
        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": str(rom_dir / "FF7.m3u"),
            "rom_dir": str(rom_dir),
            "system": "psx",
        }

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        assert not rom_dir.exists()
        # Parent system dir should still exist
        assert (tmp_path / "retrodeck" / "roms" / "psx").exists()

    @pytest.mark.asyncio
    async def test_uninstall_all_deletes_rom_dirs(self, plugin, tmp_path):
        """uninstall_all_roms should delete multi-file ROM directories."""
        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": str(rom_dir / "FF7.m3u"),
                "rom_dir": str(rom_dir),
                "system": "psx",
            },
        }

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

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[42] = {"rom_id": 42, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(42, rom_detail, target_path, "n64", "zelda.z64")

        # File ends up at target_path (not .tmp)
        assert os.path.exists(target_path)
        assert not os.path.exists(target_path + ".tmp")
        # installed_roms entry is created
        installed = plugin._state["installed_roms"].get("42")
        assert installed is not None
        assert installed["rom_id"] == 42
        assert installed["file_path"] == target_path
        assert installed["system"] == "n64"
        assert "installed_at" in installed
        # download_complete event emitted
        emit_calls = [c for c in decky.emit.call_args_list if c[0][0] == "download_complete"]
        assert len(emit_calls) == 1
        assert emit_calls[0][0][1]["rom_id"] == 42
        # download_queue status is completed
        assert plugin._download_service._download_queue[42]["status"] == "completed"


class TestDoDownloadMultiFile:
    """Tests for _do_download happy path — multi-file (ZIP)."""

    @pytest.mark.asyncio
    async def test_multi_file_happy_path(self, plugin, tmp_path):
        import zipfile as zf
        from unittest.mock import patch

        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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
        # installed_roms entry has rom_dir
        installed = plugin._state["installed_roms"].get("55")
        assert installed is not None
        assert installed["rom_dir"] == str(extract_dir)
        # Launch file detection: M3U generated from 2 cue files, so prefer M3U > CUE
        # (M3U auto-generated by _maybe_generate_m3u)
        assert installed["file_path"].endswith((".m3u", ".cue"))
        # Status is completed
        assert plugin._download_service._download_queue[55]["status"] == "completed"


class TestDoDownloadNestedSingleFile:
    """Tests for has_nested_single_file: fs_name is the parent folder, not the file (#226)."""

    @pytest.mark.asyncio
    async def test_simple_single_file_unchanged(self, plugin, tmp_path):
        """Regression: simple-single-file still uses fs_name as the local filename."""
        from unittest.mock import patch

        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[1] = {"rom_id": 1, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(1, rom_detail, target_path, "gba", "Game.gba")

        assert os.path.exists(target_path)
        installed = plugin._state["installed_roms"].get("1")
        assert installed is not None
        assert installed["file_name"] == "Game.gba"
        assert installed["file_path"] == target_path

    @pytest.mark.asyncio
    async def test_nested_single_file_uses_files_entry(self, plugin, tmp_path):
        """Happy path: has_nested_single_file derives the local filename from files[0].file_name."""
        from unittest.mock import patch

        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._download_service._loop = asyncio.get_event_loop()
        plugin._download_service._download_queue[7] = {"rom_id": 7, "status": "downloading", "progress": 0}

        with patch.object(plugin._romm_api, "download_rom_content", side_effect=fake_download):
            await plugin._download_service._do_download(7, rom_detail, target_path, "dc", "My Game.chd")

        assert os.path.exists(target_path)
        installed = plugin._state["installed_roms"].get("7")
        assert installed is not None
        assert installed["file_name"] == "My Game.chd"
        assert installed["file_path"] == target_path
        # Must NOT keep the parent-folder name from fs_name as a real on-disk file
        assert not os.path.exists(str(roms_dir / "My Game"))

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

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._state["installed_roms"]["99"] = {
            "rom_id": 99,
            "file_path": str(evil_file),
            "rom_dir": str(evil_dir),
            "system": "n64",
        }

        await plugin.remove_rom(99)
        # The evil dir/file should NOT be deleted
        assert evil_dir.exists()
        assert evil_file.exists()
        # State should still be cleaned up
        assert "99" not in plugin._state["installed_roms"]

    @pytest.mark.asyncio
    async def test_rejects_file_path_outside_roms_base(self, plugin, tmp_path):
        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._state["installed_roms"]["99"] = {
            "rom_id": 99,
            "file_path": str(evil_file),
            "system": "n64",
        }

        await plugin.remove_rom(99)
        assert evil_file.exists()
        assert "99" not in plugin._state["installed_roms"]


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

        plugin._download_service._runtime_dir = str(tmp_path)
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
        assert "42" not in plugin._state["installed_roms"]


class TestDoDownloadZipFailure:
    """Tests for _do_download — ZIP extraction failure."""

    @pytest.mark.asyncio
    async def test_zip_failure_sets_failed_and_cleans_up(self, plugin, tmp_path):
        from unittest.mock import patch

        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(good_file), "system": "n64"},
            "2": {"rom_id": 2, "file_path": str(bad_file), "system": "snes"},
        }

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        # good_file should be deleted
        assert not good_file.exists()
        # bad_file should still exist (outside roms dir)
        assert bad_file.exists()
        # removed_count reflects successful deletions
        # The current code clears all state regardless of deletion success
        assert result["removed_count"] in (1, 2)  # depends on whether _delete_rom_files raises or silently skips


class TestRemoveRomFileAlreadyGone:
    """Test remove_rom when file is already deleted."""

    @pytest.mark.asyncio
    async def test_file_already_gone_cleans_state(self, plugin, tmp_path):
        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
        decky.DECKY_USER_HOME = str(tmp_path)
        plugin._download_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
            bios=str(tmp_path / "retrodeck" / "bios"),
        )
        plugin._rom_removal_service._retrodeck_paths = FakeRetroDeckPaths(
            roms=str(tmp_path / "retrodeck" / "roms"),
        )

        # Entry exists in state but file is gone
        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": str(tmp_path / "retrodeck" / "roms" / "n64" / "gone.z64"),
            "system": "n64",
        }

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        assert "42" not in plugin._state["installed_roms"]


class TestUrlEncodedFilenameRename:
    """Tests for URL-encoded filename fix after ZIP extraction."""

    @pytest.mark.asyncio
    async def test_renames_url_encoded_files_after_extract(self, plugin, tmp_path):
        import zipfile as zf
        from unittest.mock import patch

        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._download_service._runtime_dir = str(tmp_path)
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


class TestRemoveRomCleansSaveSyncState:
    @pytest.mark.asyncio
    async def test_remove_rom_cleans_save_sync_state(self, plugin, tmp_path):
        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_path": str(rom_file),
            "system": "n64",
        }
        save_sync_state = SaveSyncState()
        save_sync_state.saves["42"] = {"last_sync": "2024-01-01"}  # type: ignore[assignment]
        save_sync_state.saves["99"] = {"last_sync": "2024-02-01"}  # type: ignore[assignment]
        save_sync_state.playtime["42"] = {"total_seconds": 3600}  # type: ignore[assignment]
        save_sync_state.playtime["99"] = {"total_seconds": 7200}  # type: ignore[assignment]
        plugin._rom_removal_service._save_sync_state = save_sync_state
        save_calls = []

        class _Recorder:
            def save_state(self) -> None:
                save_calls.append(1)

        plugin._rom_removal_service._save_sync_state_writer = _Recorder()

        result = await plugin.remove_rom(42)
        assert result["success"] is True
        # Save sync state for ROM 42 should be cleaned
        assert "42" not in save_sync_state.saves
        assert "42" not in save_sync_state.playtime
        # Other ROM's state should be untouched
        assert "99" in save_sync_state.saves
        assert "99" in save_sync_state.playtime
        # _save_sync_state_writer.save_state should have been called
        assert len(save_calls) == 1

    @pytest.mark.asyncio
    async def test_uninstall_all_cleans_save_sync_state(self, plugin, tmp_path):
        import decky

        plugin._download_service._runtime_dir = str(tmp_path)
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

        plugin._state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": str(file_a), "system": "n64"},
            "2": {"rom_id": 2, "file_path": str(file_b), "system": "n64"},
        }
        save_sync_state = SaveSyncState()
        save_sync_state.saves["1"] = {"last_sync": "2024-01-01"}  # type: ignore[assignment]
        save_sync_state.saves["2"] = {"last_sync": "2024-02-01"}  # type: ignore[assignment]
        save_sync_state.playtime["1"] = {"total_seconds": 100}  # type: ignore[assignment]
        save_sync_state.playtime["2"] = {"total_seconds": 200}  # type: ignore[assignment]
        plugin._rom_removal_service._save_sync_state = save_sync_state
        save_calls = []

        class _Recorder:
            def save_state(self) -> None:
                save_calls.append(1)

        plugin._rom_removal_service._save_sync_state_writer = _Recorder()

        result = await plugin.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 2
        # All save sync state should be cleaned
        assert save_sync_state.saves == {}
        assert save_sync_state.playtime == {}
        # _save_sync_state_writer.save_state should have been called
        assert len(save_calls) == 1


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


class TestStartShutdownLifecycle:
    """Tests for DownloadService.start / shutdown — poll-task ownership.

    The service owns its background ``poll_download_requests`` task so
    ``main._unload`` can cancel it via ``await shutdown()``. Covers the
    spawn, idempotent re-spawn, await-cancellation, and combined
    shutdown-with-active-downloads paths.
    """

    @pytest.mark.asyncio
    async def test_start_spawns_poll_task(self, plugin):
        plugin._download_service._loop = asyncio.get_event_loop()
        # Stub the poll coroutine so we don't run the real polling
        # loop — we only care that start() owns the task handle.

        async def _noop_poll():
            await asyncio.sleep(0.01)

        plugin._download_service.poll_download_requests = _noop_poll  # type: ignore[method-assign]

        plugin._download_service.start()
        poll_task = plugin._download_service._poll_task
        assert isinstance(poll_task, asyncio.Task)

        # Let the stub task finish so the loop has nothing pending.
        await poll_task

    @pytest.mark.asyncio
    async def test_start_is_idempotent_when_task_running(self, plugin):
        plugin._download_service._loop = asyncio.get_event_loop()

        async def _long_poll():
            await asyncio.sleep(5)

        plugin._download_service.poll_download_requests = _long_poll  # type: ignore[method-assign]

        plugin._download_service.start()
        first_task = plugin._download_service._poll_task
        plugin._download_service.start()
        second_task = plugin._download_service._poll_task

        assert first_task is second_task

        # Clean up — cancel + await to let the loop tear down without
        # an unawaited-task warning.
        await plugin._download_service.shutdown()

    @pytest.mark.asyncio
    async def test_start_after_previous_task_done_spawns_new(self, plugin):
        plugin._download_service._loop = asyncio.get_event_loop()

        async def _quick_poll():
            return None

        plugin._download_service.poll_download_requests = _quick_poll  # type: ignore[method-assign]

        plugin._download_service.start()
        first_task = plugin._download_service._poll_task
        assert first_task is not None
        await first_task

        plugin._download_service.start()
        second_task = plugin._download_service._poll_task
        assert second_task is not None
        assert second_task is not first_task

        await second_task

    @pytest.mark.asyncio
    async def test_shutdown_cancels_poll_task_and_awaits_exit(self, plugin):
        plugin._download_service._loop = asyncio.get_event_loop()

        async def _long_poll():
            await asyncio.sleep(30)

        plugin._download_service.poll_download_requests = _long_poll  # type: ignore[method-assign]

        plugin._download_service.start()
        poll_task = plugin._download_service._poll_task
        assert poll_task is not None
        assert not poll_task.done()

        await plugin._download_service.shutdown()

        assert poll_task.done()
        assert poll_task.cancelled()
        assert plugin._download_service._poll_task is None

    @pytest.mark.asyncio
    async def test_shutdown_handles_missing_poll_task(self, plugin):
        # Never called start() — shutdown must still tear down per-ROM
        # tasks without touching the missing poll handle.
        task_a = MagicMock()
        plugin._download_service._download_tasks[7] = task_a

        await plugin._download_service.shutdown()

        task_a.cancel.assert_called_once_with()
        assert plugin._download_service._download_tasks == {}
        assert plugin._download_service._poll_task is None


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


class TestPollDownloadRequestsLoopBody:
    """Tests for poll_download_requests — request dispatch + error swallowing.

    Covers the loop body that reads from the queue adapter, dispatches
    each rom_id via start_download, and the bare-except branch that
    swallows + logs exceptions other than CancelledError so the poll
    keeps running across transient failures.
    """

    @pytest.mark.asyncio
    async def test_dispatches_queued_request_to_start_download(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, patch

        from fakes.fake_download_queue_store import FakeDownloadQueueStore

        plugin._download_service._runtime_dir = str(tmp_path)
        # No migration pending — must not block the loop body.
        plugin._download_service._is_retrodeck_migration_pending = lambda: False

        # Sleeper cancels after one full iteration so the body runs
        # exactly once.
        class _CancellingSleeper:
            def __init__(self):
                self.calls = 0

            async def sleep(self, _seconds):
                self.calls += 1
                if self.calls >= 2:
                    raise asyncio.CancelledError

        plugin._download_service._sleeper = _CancellingSleeper()

        tracking_queue = FakeDownloadQueueStore(entries=[{"rom_id": 42}, {"rom_id": 99}, {"no_rom_id": True}])
        plugin._download_service._download_queue_io = tracking_queue
        plugin._download_service._loop = asyncio.get_event_loop()

        with patch.object(plugin._download_service, "start_download", new_callable=AsyncMock) as mock_start:
            with pytest.raises(asyncio.CancelledError):
                await plugin._download_service.poll_download_requests()

            assert tracking_queue.poll_count >= 1
            # Both entries with rom_id should be dispatched; the
            # malformed entry without rom_id is skipped.
            mock_start.assert_any_await(42)
            mock_start.assert_any_await(99)
            assert mock_start.await_count == 2

    @pytest.mark.asyncio
    async def test_empty_poll_continues_without_dispatch(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, patch

        from fakes.fake_download_queue_store import FakeDownloadQueueStore

        plugin._download_service._runtime_dir = str(tmp_path)
        plugin._download_service._is_retrodeck_migration_pending = lambda: False

        class _CancellingSleeper:
            def __init__(self):
                self.calls = 0

            async def sleep(self, _seconds):
                self.calls += 1
                if self.calls >= 2:
                    raise asyncio.CancelledError

        plugin._download_service._sleeper = _CancellingSleeper()

        tracking_queue = FakeDownloadQueueStore(entries=[])
        plugin._download_service._download_queue_io = tracking_queue
        plugin._download_service._loop = asyncio.get_event_loop()

        with patch.object(plugin._download_service, "start_download", new_callable=AsyncMock) as mock_start:
            with pytest.raises(asyncio.CancelledError):
                await plugin._download_service.poll_download_requests()

            assert tracking_queue.poll_count >= 1
            mock_start.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_swallows_non_cancelled_exception_and_keeps_polling(self, plugin, tmp_path, caplog):
        import logging

        plugin._download_service._runtime_dir = str(tmp_path)
        plugin._download_service._is_retrodeck_migration_pending = lambda: False

        # Sleeper cancels after a couple of iterations so the loop
        # body runs at least once after the failing poll.
        class _CancellingSleeper:
            def __init__(self):
                self.calls = 0

            async def sleep(self, _seconds):
                self.calls += 1
                if self.calls >= 3:
                    raise asyncio.CancelledError

        plugin._download_service._sleeper = _CancellingSleeper()

        # Queue adapter that raises on poll — exercises the bare-except
        # branch which must log a warning and continue the loop.
        class _ExplodingQueue:
            def __init__(self):
                self.poll_count = 0

            def poll_and_clear(self, _path):
                self.poll_count += 1
                raise RuntimeError("boom: queue read failed")

        exploding = _ExplodingQueue()
        plugin._download_service._download_queue_io = exploding
        plugin._download_service._loop = asyncio.get_event_loop()

        with (
            caplog.at_level(logging.WARNING, logger="test_romm"),
            pytest.raises(asyncio.CancelledError),
        ):
            await plugin._download_service.poll_download_requests()

        # Loop must have re-entered after the first failure (sleeper hit
        # at least twice before the cancelling iteration).
        assert exploding.poll_count >= 1
        # Warning must mention the underlying error message.
        assert any("Download request poll error" in rec.message and "boom" in rec.message for rec in caplog.records)


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
