import pytest
import os
import asyncio

# conftest.py patches decky before this import
from main import Plugin


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._sync_running = False
    p._sync_cancel = False
    p._sync_progress = {"running": False}
    p._state = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None, "sync_stats": {}}
    p._pending_sync = {}
    p._download_tasks = {}
    p._download_queue = {}
    p._download_in_progress = set()
    p._metadata_cache = {}
    return p


class TestFirmwareDestPath:
    """Tests for _firmware_dest_path — BIOS destination mapping."""

    def test_flat_default(self, plugin, tmp_path):
        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        fw = {"file_name": "bios.bin", "file_path": "bios/n64/bios.bin"}
        dest = plugin._firmware_dest_path(fw)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "bios.bin")

    def test_dreamcast_subfolder(self, plugin, tmp_path):
        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        fw = {"file_name": "dc_boot.bin", "file_path": "bios/dc/dc_boot.bin"}
        dest = plugin._firmware_dest_path(fw)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "dc", "dc_boot.bin")

    def test_ps2_subfolder(self, plugin, tmp_path):
        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        fw = {"file_name": "scph10000.bin", "file_path": "bios/ps2/scph10000.bin"}
        dest = plugin._firmware_dest_path(fw)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "pcsx2", "bios", "scph10000.bin")

    def test_unknown_platform_flat(self, plugin, tmp_path):
        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        fw = {"file_name": "fw.bin", "file_path": "bios/saturn/fw.bin"}
        dest = plugin._firmware_dest_path(fw)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "fw.bin")


class TestGetFirmwareStatus:
    @pytest.mark.asyncio
    async def test_returns_grouped_platforms(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        firmware_list = [
            {"id": 1, "file_name": "bios_dc.bin", "file_path": "bios/dc/bios_dc.bin", "file_size_bytes": 2048, "md5_hash": "abc123"},
            {"id": 2, "file_name": "flash_dc.bin", "file_path": "bios/dc/flash_dc.bin", "file_size_bytes": 1024, "md5_hash": "def456"},
            {"id": 3, "file_name": "scph.bin", "file_path": "bios/ps2/scph.bin", "file_size_bytes": 4096, "md5_hash": ""},
        ]

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(return_value=firmware_list)

        result = await plugin.get_firmware_status()
        assert result["success"] is True
        assert len(result["platforms"]) == 2

        dc_plat = next(p for p in result["platforms"] if p["platform_slug"] == "dc")
        assert len(dc_plat["files"]) == 2
        assert all(not f["downloaded"] for f in dc_plat["files"])

    @pytest.mark.asyncio
    async def test_detects_downloaded_files(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock
        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        bios_dir = tmp_path / "retrodeck" / "bios" / "dc"
        bios_dir.mkdir(parents=True)
        (bios_dir / "bios_dc.bin").write_bytes(b"\x00" * 100)

        firmware_list = [
            {"id": 1, "file_name": "bios_dc.bin", "file_path": "bios/dc/bios_dc.bin", "file_size_bytes": 100, "md5_hash": ""},
        ]

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(return_value=firmware_list)

        result = await plugin.get_firmware_status()
        assert result["success"] is True
        assert result["platforms"][0]["files"][0]["downloaded"] is True

    @pytest.mark.asyncio
    async def test_handles_api_error(self, plugin):
        from unittest.mock import AsyncMock, MagicMock

        plugin.loop = MagicMock()
        plugin.loop.run_in_executor = AsyncMock(side_effect=Exception("404 Not Found"))

        result = await plugin.get_firmware_status()
        assert result["success"] is False
        assert "Failed" in result["message"]


class TestDownloadFirmware:
    @pytest.mark.asyncio
    async def test_downloads_and_verifies_md5(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch
        import hashlib
        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        content = b"firmware data here"
        expected_md5 = hashlib.md5(content).hexdigest()

        fw_detail = {
            "id": 10,
            "file_name": "bios.bin",
            "file_path": "bios/n64/bios.bin",
            "file_size_bytes": len(content),
            "md5_hash": expected_md5,
        }

        def fake_download(path, dest, progress_callback=None):
            with open(dest, "wb") as f:
                f.write(content)

        plugin.loop = asyncio.get_event_loop()

        with patch.object(plugin, "_romm_request", return_value=fw_detail), \
             patch.object(plugin, "_romm_download", side_effect=fake_download):
            result = await plugin.download_firmware(10)

        assert result["success"] is True
        assert result["md5_match"] is True
        assert os.path.exists(result["file_path"])

    @pytest.mark.asyncio
    async def test_handles_download_error(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch
        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        fw_detail = {
            "id": 10,
            "file_name": "bios.bin",
            "file_path": "bios/n64/bios.bin",
            "file_size_bytes": 100,
            "md5_hash": "",
        }

        plugin.loop = asyncio.get_event_loop()

        with patch.object(plugin, "_romm_request", return_value=fw_detail), \
             patch.object(plugin, "_romm_download", side_effect=IOError("Connection reset")):
            result = await plugin.download_firmware(10)

        assert result["success"] is False
        assert "Download failed" in result["message"]


class TestDownloadAllFirmware:
    @pytest.mark.asyncio
    async def test_downloads_missing_only(self, plugin, tmp_path):
        from unittest.mock import AsyncMock, MagicMock, patch
        import decky
        decky.DECKY_USER_HOME = str(tmp_path)

        # Pre-create one file so it's skipped
        bios_dir = tmp_path / "retrodeck" / "bios" / "dc"
        bios_dir.mkdir(parents=True)
        (bios_dir / "existing.bin").write_bytes(b"\x00" * 50)

        firmware_list = [
            {"id": 1, "file_name": "existing.bin", "file_path": "bios/dc/existing.bin", "file_size_bytes": 50, "md5_hash": ""},
            {"id": 2, "file_name": "missing.bin", "file_path": "bios/dc/missing.bin", "file_size_bytes": 100, "md5_hash": ""},
        ]

        plugin.loop = asyncio.get_event_loop()

        download_called_ids = []

        async def fake_download_firmware(fw_id):
            download_called_ids.append(fw_id)
            return {"success": True}

        with patch.object(plugin, "_romm_request", return_value=firmware_list), \
             patch.object(plugin, "download_firmware", side_effect=fake_download_firmware):
            result = await plugin.download_all_firmware("dc")

        assert result["success"] is True
        assert result["downloaded"] == 1
        assert 2 in download_called_ids
        assert 1 not in download_called_ids
