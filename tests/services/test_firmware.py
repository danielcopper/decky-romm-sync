import asyncio
import os
import time
from unittest.mock import MagicMock, patch

import pytest
from models.bios import BiosFileEntry

from adapters.steam_config import SteamConfigAdapter

# conftest.py patches decky before this import
from main import Plugin
from services.firmware import FirmwareService
from services.library import LibraryService


@pytest.fixture
def plugin():
    p = Plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()
    p._state = {
        "shortcut_registry": {},
        "installed_roms": {},
        "last_sync": None,
        "sync_stats": {},
        "downloaded_bios": {},
        "retrodeck_home_path": "",
    }
    p._metadata_cache = {}

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._firmware_service = FirmwareService(
        romm_api=p._romm_api,
        state=p._state,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        plugin_dir=decky.DECKY_PLUGIN_DIR,
        save_state=MagicMock(),
        save_firmware_cache=MagicMock(),
        load_firmware_cache=MagicMock(return_value={}),
        get_bios_path=MagicMock(return_value=""),
    )

    p._sync_service = LibraryService(
        romm_api=p._romm_api,
        steam_config=steam_config,
        state=p._state,
        settings=p.settings,
        metadata_cache=p._metadata_cache,
        loop=asyncio.get_event_loop(),
        logger=decky.logger,
        plugin_dir=decky.DECKY_PLUGIN_DIR,
        emit=decky.emit,
        save_state=p._save_state,
        save_settings_to_disk=p._save_settings_to_disk,
        log_debug=p._log_debug,
    )
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin, fw):
    """Ensure plugin.loop and fw._loop match the running event loop for async tests."""
    loop = asyncio.get_event_loop()
    plugin.loop = loop
    fw._loop = loop


# Shorthand to access the firmware service from plugin
@pytest.fixture
def fw(plugin):
    return plugin._firmware_service


class TestFirmwareDestPath:
    """Tests for _firmware_dest_path — registry-based BIOS destination mapping."""

    def test_flat_default_no_registry(self, fw, tmp_path):
        """File not in registry goes flat in bios root."""
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        fw._get_bios_path = MagicMock(return_value=bios)
        firmware = {"file_name": "bios.bin", "file_path": "bios/n64/bios.bin"}
        dest = fw._firmware_dest_path(firmware)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "bios.bin")

    def test_dreamcast_subfolder_from_registry(self, fw, tmp_path):
        """Registry firmware_path with subdirectory places file correctly."""
        fw._bios_files_index["dc_boot.bin"] = {
            "description": "Dreamcast BIOS",
            "required": True,
            "firmware_path": "dc/dc_boot.bin",
            "platform": "dc",
        }

        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        fw._get_bios_path = MagicMock(return_value=bios)
        firmware = {"file_name": "dc_boot.bin", "file_path": "bios/dc/dc_boot.bin"}
        dest = fw._firmware_dest_path(firmware)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "dc", "dc_boot.bin")

    def test_psx_flat_from_registry(self, fw, tmp_path):
        """Registry firmware_path without subdirectory goes flat."""

        fw._bios_files_index["scph5501.bin"] = {
            "description": "PS1 US BIOS",
            "required": True,
            "firmware_path": "scph5501.bin",
            "platform": "psx",
        }

        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        with patch.object(fw, "_get_bios_path", return_value=bios):
            firmware = {"file_name": "scph5501.bin", "file_path": "bios/ps/scph5501.bin"}
            dest = fw._firmware_dest_path(firmware)
            assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "scph5501.bin")

    def test_uses_dynamic_bios_path(self, fw, tmp_path):
        """Uses retrodeck_config.get_bios_path() for the base directory."""

        sd_bios = "/run/media/deck/Emulation/retrodeck/bios"
        with patch.object(fw, "_get_bios_path", return_value=sd_bios):
            firmware = {"file_name": "fw.bin", "file_path": "bios/saturn/fw.bin"}
            dest = fw._firmware_dest_path(firmware)
            assert dest == os.path.join(sd_bios, "fw.bin")

    def test_unknown_file_flat_fallback(self, fw, tmp_path):
        """File not in registry falls back to flat in bios root."""

        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        with patch.object(fw, "_get_bios_path", return_value=bios):
            firmware = {"file_name": "fw.bin", "file_path": "bios/saturn/fw.bin"}
            dest = fw._firmware_dest_path(firmware)
            assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "fw.bin")


class TestGetFirmwareStatus:
    @pytest.mark.asyncio
    async def test_returns_grouped_platforms(self, fw, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        firmware_list = [
            {
                "id": 1,
                "file_name": "bios_dc.bin",
                "file_path": "bios/dc/bios_dc.bin",
                "file_size_bytes": 2048,
                "md5_hash": "abc123",
            },
            {
                "id": 2,
                "file_name": "flash_dc.bin",
                "file_path": "bios/dc/flash_dc.bin",
                "file_size_bytes": 1024,
                "md5_hash": "def456",
            },
            {
                "id": 3,
                "file_name": "scph.bin",
                "file_path": "bios/ps2/scph.bin",
                "file_size_bytes": 4096,
                "md5_hash": "",
            },
        ]

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        result = await fw.get_firmware_status()
        assert result["success"] is True
        assert len(result["platforms"]) == 2

        dc_plat = next(p for p in result["platforms"] if p["platform_slug"] == "dc")
        assert len(dc_plat["files"]) == 2
        assert all(not f["downloaded"] for f in dc_plat["files"])  # get_firmware_status files are dicts

    @pytest.mark.asyncio
    async def test_detects_downloaded_files(self, fw, tmp_path):
        from unittest.mock import AsyncMock, MagicMock

        # File goes flat in bios root (not in registry, no firmware_path)
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        (bios_dir / "bios_dc.bin").write_bytes(b"\x00" * 100)

        firmware_list = [
            {
                "id": 1,
                "file_name": "bios_dc.bin",
                "file_path": "bios/dc/bios_dc.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        with patch.object(fw, "_get_bios_path", return_value=str(bios_dir)):
            result = await fw.get_firmware_status()
        assert result["success"] is True
        assert result["platforms"][0]["files"][0]["downloaded"] is True

    @pytest.mark.asyncio
    async def test_handles_api_error_with_offline_fallback(self, fw):
        from unittest.mock import AsyncMock, MagicMock

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(side_effect=Exception("Connection refused"))

        result = await fw.get_firmware_status()
        assert result["success"] is True
        assert result["server_offline"] is True
        assert "platforms" in result


class TestDownloadFirmware:
    @pytest.mark.asyncio
    async def test_downloads_and_verifies_md5(self, plugin, fw, tmp_path):
        import hashlib

        content = b"firmware data here"
        expected_md5 = hashlib.md5(content).hexdigest()

        fw_detail = {
            "id": 10,
            "file_name": "bios.bin",
            "file_path": "bios/n64/bios.bin",
            "file_size_bytes": len(content),
            "md5_hash": expected_md5,
        }

        bios_dir = tmp_path / "bios"
        bios_dir.mkdir()

        def fake_download(firmware_id, filename, dest):
            with open(dest, "wb") as f:
                f.write(content)

        fw._get_bios_path = MagicMock(return_value=str(bios_dir))
        fw._loop = asyncio.get_event_loop()

        with (
            patch.object(plugin._romm_api, "get_firmware", return_value=fw_detail),
            patch.object(plugin._romm_api, "download_firmware", side_effect=fake_download),
        ):
            result = await fw.download_firmware(10)

        assert result["success"] is True
        assert result["md5_match"] is True
        assert os.path.exists(result["file_path"])
        # Verify state tracking
        assert "bios.bin" in plugin._state["downloaded_bios"]
        assert plugin._state["downloaded_bios"]["bios.bin"]["firmware_id"] == 10

    @pytest.mark.asyncio
    async def test_handles_download_error(self, plugin, fw, tmp_path):

        fw_detail = {
            "id": 10,
            "file_name": "bios.bin",
            "file_path": "bios/n64/bios.bin",
            "file_size_bytes": 100,
            "md5_hash": "",
        }

        fw._loop = asyncio.get_event_loop()

        with (
            patch.object(plugin._romm_api, "get_firmware", return_value=fw_detail),
            patch.object(plugin._romm_api, "download_firmware", side_effect=OSError("Connection reset")),
        ):
            result = await fw.download_firmware(10)

        assert result["success"] is False
        assert "error_code" in result


class TestDownloadAllFirmware:
    @pytest.mark.asyncio
    async def test_downloads_missing_only(self, plugin, fw, tmp_path):

        # Pre-create one file so it's skipped (flat in bios root, not in registry)
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        (bios_dir / "existing.bin").write_bytes(b"\x00" * 50)

        firmware_list = [
            {
                "id": 1,
                "file_name": "existing.bin",
                "file_path": "bios/dc/existing.bin",
                "file_size_bytes": 50,
                "md5_hash": "",
            },
            {
                "id": 2,
                "file_name": "missing.bin",
                "file_path": "bios/dc/missing.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]

        fw._loop = asyncio.get_event_loop()

        download_called_ids = []

        async def fake_download_firmware(fw_id):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw, "download_firmware", side_effect=fake_download_firmware),
            patch.object(fw, "_get_bios_path", return_value=str(bios_dir)),
        ):
            result = await fw.download_all_firmware("dc")

        assert result["success"] is True
        assert result["downloaded"] == 1
        assert 2 in download_called_ids
        assert 1 not in download_called_ids


class TestDeletePlatformBios:
    @pytest.mark.asyncio
    async def test_delete_platform_bios_happy_path(self, plugin, fw, tmp_path):
        """Deleting platform BIOS removes downloaded files and state entries."""
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        bios_file = bios_dir / "scph5501.bin"
        bios_file.write_bytes(b"\x00" * 512)

        # Pre-populate state tracking
        plugin._state["downloaded_bios"]["scph5501.bin"] = {
            "file_path": str(bios_file),
            "firmware_id": 42,
            "platform_slug": "psx",
        }

        # Mock check_platform_bios to return our test file
        async def mock_check(slug, rom_filename=None):
            return {
                "needs_bios": True,
                "server_count": 1,
                "local_count": 1,
                "all_downloaded": True,
                "files": (
                    BiosFileEntry(
                        file_name="scph5501.bin",
                        downloaded=True,
                        local_path=str(bios_file),
                        required=True,
                        description="PS1 BIOS",
                        classification="required",
                        cores={},
                        used_by_active=True,
                    ),
                ),
            }

        fw.check_platform_bios = mock_check

        result = await fw.delete_platform_bios("psx")
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not bios_file.exists()
        # Verify state entry removed
        assert "scph5501.bin" not in plugin._state["downloaded_bios"]

    @pytest.mark.asyncio
    async def test_delete_platform_bios_no_files(self, fw):
        """Deleting BIOS when none exist returns success with 0."""

        async def mock_check(slug, rom_filename=None):
            return {"needs_bios": False}

        fw.check_platform_bios = mock_check

        result = await fw.delete_platform_bios("snes")
        assert result["success"] is True
        assert result["deleted_count"] == 0

    @pytest.mark.asyncio
    async def test_delete_platform_bios_skips_not_downloaded(self, fw, tmp_path):
        """Only files with downloaded=True are deleted."""

        async def mock_check(slug, rom_filename=None):
            return {
                "needs_bios": True,
                "server_count": 2,
                "local_count": 0,
                "all_downloaded": False,
                "files": (
                    BiosFileEntry(
                        file_name="bios1.bin",
                        downloaded=False,
                        local_path="/fake/path1",
                        required=False,
                        description="bios1.bin",
                        classification="unknown",
                        cores={},
                        used_by_active=True,
                    ),
                    BiosFileEntry(
                        file_name="bios2.bin",
                        downloaded=False,
                        local_path="/fake/path2",
                        required=False,
                        description="bios2.bin",
                        classification="unknown",
                        cores={},
                        used_by_active=True,
                    ),
                ),
            }

        fw.check_platform_bios = mock_check

        result = await fw.delete_platform_bios("psx")
        assert result["success"] is True
        assert result["deleted_count"] == 0


class TestBiosRegistry:
    def test_load_bios_registry(self, fw, tmp_path):
        """Loads registry JSON and verifies structure + _bios_files_index."""
        import json

        registry_data = {
            "_meta": {"version": "2.0.0", "description": "Test registry"},
            "platforms": {
                "psx": {
                    "bios.bin": {
                        "description": "Main BIOS",
                        "required": True,
                        "md5": "abc123",
                        "sha1": "def456",
                        "size": 2048,
                    },
                },
                "dc": {
                    "optional.bin": {
                        "description": "Optional firmware",
                        "required": False,
                        "md5": "789abc",
                        "sha1": "012def",
                        "size": 1024,
                    },
                },
            },
        }

        defaults_dir = tmp_path / "defaults"
        defaults_dir.mkdir()
        registry_file = defaults_dir / "bios_registry.json"
        registry_file.write_text(json.dumps(registry_data))

        fw._plugin_dir = str(tmp_path)
        fw.load_bios_registry()

        assert "_meta" in fw._bios_registry
        assert "platforms" in fw._bios_registry
        assert "psx" in fw._bios_registry["platforms"]
        assert "bios.bin" in fw._bios_registry["platforms"]["psx"]
        assert fw._bios_registry["platforms"]["psx"]["bios.bin"]["required"] is True
        assert "dc" in fw._bios_registry["platforms"]
        assert fw._bios_registry["platforms"]["dc"]["optional.bin"]["required"] is False
        # Verify _bios_files_index is populated
        assert "bios.bin" in fw._bios_files_index
        assert fw._bios_files_index["bios.bin"]["platform"] == "psx"
        assert "optional.bin" in fw._bios_files_index
        assert fw._bios_files_index["optional.bin"]["platform"] == "dc"

    def test_load_bios_registry_missing_file(self, fw):
        """When registry file doesn't exist, returns empty dict."""
        fw._plugin_dir = "/nonexistent"
        fw.load_bios_registry()

        assert fw._bios_registry == {}

    def test_enrich_firmware_required(self, fw):
        """File in registry marked required=True."""
        fw._bios_files_index = {
            "scph5501.bin": {
                "description": "PS1 BIOS (USA)",
                "required": True,
                "md5": "abc123",
                "platform": "psx",
            },
        }
        file_dict = {"file_name": "scph5501.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict)
        assert result["required"] is True
        assert result["description"] == "PS1 BIOS (USA)"

    def test_enrich_firmware_optional(self, fw):
        """File in registry marked required=False."""
        fw._bios_files_index = {
            "optional_fw.bin": {
                "description": "Optional debug firmware",
                "required": False,
                "md5": "",
                "platform": "dc",
            },
        }
        file_dict = {"file_name": "optional_fw.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict)
        assert result["required"] is False
        assert result["description"] == "Optional debug firmware"

    def test_enrich_firmware_unknown_defaults_not_required(self, fw):
        """File NOT in registry defaults to required=False (unknown classification)."""
        fw._bios_files_index = {}
        file_dict = {"file_name": "unknown_bios.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict)
        assert result["required"] is False
        assert result["classification"] == "unknown"
        assert result["description"] == "unknown_bios.bin"

    def test_enrich_firmware_unknown_classification(self, fw):
        """File NOT in registry gets classification 'unknown'."""
        fw._bios_files_index = {}
        file_dict = {"file_name": "mystery.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict)
        assert result["classification"] == "unknown"

    def test_enrich_firmware_required_classification(self, fw):
        """File in registry with required=True gets classification 'required'."""
        fw._bios_files_index = {
            "scph5501.bin": {
                "description": "PS1 BIOS",
                "required": True,
                "md5": "",
                "platform": "psx",
            },
        }
        file_dict = {"file_name": "scph5501.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict)
        assert result["classification"] == "required"

    def test_enrich_firmware_optional_classification(self, fw):
        """File in registry with required=False gets classification 'optional'."""
        fw._bios_files_index = {
            "optional_fw.bin": {
                "description": "Optional firmware",
                "required": False,
                "md5": "",
                "platform": "dc",
            },
        }
        file_dict = {"file_name": "optional_fw.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict)
        assert result["classification"] == "optional"

    def test_hash_validation_match(self, fw):
        """RomM md5 matches registry md5."""
        fw._bios_files_index = {
            "bios.bin": {
                "description": "Test BIOS",
                "required": True,
                "md5": "abc123def456",
                "platform": "dc",
            },
        }
        file_dict = {"file_name": "bios.bin", "md5": "abc123def456"}
        result = fw._enrich_firmware_file(file_dict)
        assert result["hash_valid"] is True

    def test_hash_validation_mismatch(self, fw):
        """RomM md5 differs from registry md5."""
        fw._bios_files_index = {
            "bios.bin": {
                "description": "Test BIOS",
                "required": True,
                "md5": "abc123def456",
                "platform": "dc",
            },
        }
        file_dict = {"file_name": "bios.bin", "md5": "000000000000"}
        result = fw._enrich_firmware_file(file_dict)
        assert result["hash_valid"] is False

    def test_hash_validation_null(self, fw):
        """No hash from either source results in hash_valid=None."""
        fw._bios_files_index = {
            "bios.bin": {
                "description": "Test BIOS",
                "required": True,
                "md5": "",
                "platform": "dc",
            },
        }
        file_dict = {"file_name": "bios.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict)
        assert result["hash_valid"] is None

    def test_hash_validation_null_no_registry_entry(self, fw):
        """File not in registry and no RomM hash -> hash_valid=None."""
        fw._bios_files_index = {}
        file_dict = {"file_name": "bios.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict)
        assert result["hash_valid"] is None

    def test_hash_validation_case_insensitive(self, fw):
        """Hash comparison is case-insensitive."""
        fw._bios_files_index = {
            "bios.bin": {
                "description": "Test BIOS",
                "required": True,
                "md5": "ABC123DEF456",
                "platform": "dc",
            },
        }
        file_dict = {"file_name": "bios.bin", "md5": "abc123def456"}
        result = fw._enrich_firmware_file(file_dict)
        assert result["hash_valid"] is True


class TestCheckPlatformBiosRequired:
    @pytest.mark.asyncio
    async def test_required_counts(self, fw, tmp_path):
        """check_platform_bios includes required_count/required_downloaded."""
        from unittest.mock import AsyncMock, MagicMock

        firmware_list = [
            {
                "id": 1,
                "file_name": "required1.bin",
                "file_path": "bios/dc/required1.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
            {
                "id": 2,
                "file_name": "required2.bin",
                "file_path": "bios/dc/required2.bin",
                "file_size_bytes": 200,
                "md5_hash": "",
            },
            {
                "id": 3,
                "file_name": "optional1.bin",
                "file_path": "bios/dc/optional1.bin",
                "file_size_bytes": 300,
                "md5_hash": "",
            },
        ]

        fw._bios_registry = {
            "platforms": {
                "dc": {
                    "required1.bin": {"description": "Required BIOS 1", "required": True, "md5": ""},
                    "required2.bin": {"description": "Required BIOS 2", "required": True, "md5": ""},
                    "optional1.bin": {"description": "Optional firmware", "required": False, "md5": ""},
                },
            },
        }
        fw._bios_files_index = {
            "required1.bin": {"description": "Required BIOS 1", "required": True, "md5": "", "platform": "dc"},
            "required2.bin": {"description": "Required BIOS 2", "required": True, "md5": "", "platform": "dc"},
            "optional1.bin": {"description": "Optional firmware", "required": False, "md5": "", "platform": "dc"},
        }

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        result = await fw.check_platform_bios("dc")
        assert result["needs_bios"] is True
        assert result["required_count"] == 2
        assert result["required_downloaded"] == 0
        assert result["server_count"] == 3

    @pytest.mark.asyncio
    async def test_all_required_downloaded(self, fw, tmp_path):
        """When all required files are downloaded, counts reflect this."""
        from unittest.mock import AsyncMock, MagicMock

        # Create downloaded required files (flat in bios root, no firmware_path in registry)
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        (bios_dir / "required1.bin").write_bytes(b"\x00" * 100)
        (bios_dir / "required2.bin").write_bytes(b"\x00" * 200)
        # Leave optional1.bin not downloaded

        firmware_list = [
            {
                "id": 1,
                "file_name": "required1.bin",
                "file_path": "bios/dc/required1.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
            {
                "id": 2,
                "file_name": "required2.bin",
                "file_path": "bios/dc/required2.bin",
                "file_size_bytes": 200,
                "md5_hash": "",
            },
            {
                "id": 3,
                "file_name": "optional1.bin",
                "file_path": "bios/dc/optional1.bin",
                "file_size_bytes": 300,
                "md5_hash": "",
            },
        ]

        fw._bios_registry = {
            "platforms": {
                "dc": {
                    "required1.bin": {"description": "Required BIOS 1", "required": True, "md5": ""},
                    "required2.bin": {"description": "Required BIOS 2", "required": True, "md5": ""},
                    "optional1.bin": {"description": "Optional firmware", "required": False, "md5": ""},
                },
            },
        }
        fw._bios_files_index = {
            "required1.bin": {"description": "Required BIOS 1", "required": True, "md5": "", "platform": "dc"},
            "required2.bin": {"description": "Required BIOS 2", "required": True, "md5": "", "platform": "dc"},
            "optional1.bin": {"description": "Optional firmware", "required": False, "md5": "", "platform": "dc"},
        }

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        with patch.object(fw, "_get_bios_path", return_value=str(bios_dir)):
            result = await fw.check_platform_bios("dc")
        assert result["needs_bios"] is True
        assert result["required_count"] == 2
        assert result["required_downloaded"] == 2
        assert result["local_count"] == 2
        # all_downloaded is False because optional1.bin is not downloaded
        assert result["all_downloaded"] is False

    @pytest.mark.asyncio
    async def test_per_file_required_and_description(self, fw, tmp_path):
        """Individual files include required and description from registry."""
        from unittest.mock import AsyncMock, MagicMock

        firmware_list = [
            {"id": 1, "file_name": "bios.bin", "file_path": "bios/dc/bios.bin", "file_size_bytes": 100, "md5_hash": ""},
        ]

        fw._bios_registry = {
            "platforms": {
                "dc": {
                    "bios.bin": {"description": "Dreamcast BIOS", "required": True, "md5": ""},
                },
            },
        }
        fw._bios_files_index = {
            "bios.bin": {"description": "Dreamcast BIOS", "required": True, "md5": "", "platform": "dc"},
        }

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        result = await fw.check_platform_bios("dc")
        assert result["files"][0]["required"] is True
        assert result["files"][0]["description"] == "Dreamcast BIOS"

    @pytest.mark.asyncio
    async def test_check_platform_bios_unknown_count(self, fw, tmp_path):
        """RomM has files not in registry -> unknown_count > 0."""
        from unittest.mock import AsyncMock, MagicMock

        firmware_list = [
            {
                "id": 1,
                "file_name": "known.bin",
                "file_path": "bios/dc/known.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
            {
                "id": 2,
                "file_name": "mystery.bin",
                "file_path": "bios/dc/mystery.bin",
                "file_size_bytes": 200,
                "md5_hash": "",
            },
            {
                "id": 3,
                "file_name": "alien.bin",
                "file_path": "bios/dc/alien.bin",
                "file_size_bytes": 300,
                "md5_hash": "",
            },
        ]

        # Only "known.bin" is in the registry
        fw._bios_registry = {
            "platforms": {
                "dc": {
                    "known.bin": {"description": "Known BIOS", "required": True, "md5": ""},
                },
            },
        }
        fw._bios_files_index = {
            "known.bin": {"description": "Known BIOS", "required": True, "md5": "", "platform": "dc"},
        }

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        result = await fw.check_platform_bios("dc")
        assert result["needs_bios"] is True
        assert result["unknown_count"] == 2
        # Per-file classification
        classifications = {f["file_name"]: f["classification"] for f in result["files"]}
        assert classifications["known.bin"] == "required"
        assert classifications["mystery.bin"] == "unknown"
        assert classifications["alien.bin"] == "unknown"


class TestDownloadRequiredFirmware:
    @pytest.mark.asyncio
    async def test_downloads_required_only(self, plugin, fw, tmp_path):
        """Only downloads files marked required, skips optional."""

        firmware_list = [
            {
                "id": 1,
                "file_name": "required.bin",
                "file_path": "bios/dc/required.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
            {
                "id": 2,
                "file_name": "optional.bin",
                "file_path": "bios/dc/optional.bin",
                "file_size_bytes": 200,
                "md5_hash": "",
            },
        ]

        fw._bios_registry = {
            "platforms": {
                "dc": {
                    "required.bin": {"description": "Required BIOS", "required": True, "md5": ""},
                    "optional.bin": {"description": "Optional firmware", "required": False, "md5": ""},
                },
            },
        }
        fw._bios_files_index = {
            "required.bin": {"description": "Required BIOS", "required": True, "md5": "", "platform": "dc"},
            "optional.bin": {"description": "Optional firmware", "required": False, "md5": "", "platform": "dc"},
        }

        fw._loop = asyncio.get_event_loop()

        download_called_ids = []

        async def fake_download_firmware(fw_id):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw, "download_firmware", side_effect=fake_download_firmware),
        ):
            result = await fw.download_required_firmware("dc")

        assert result["success"] is True
        assert result["downloaded"] == 1
        assert 1 in download_called_ids
        assert 2 not in download_called_ids

    @pytest.mark.asyncio
    async def test_skips_already_downloaded_required(self, plugin, fw, tmp_path):
        """Skips required files that are already downloaded."""

        # Pre-create one required file so it's skipped (flat in bios root)
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        (bios_dir / "existing.bin").write_bytes(b"\x00" * 100)

        firmware_list = [
            {
                "id": 1,
                "file_name": "existing.bin",
                "file_path": "bios/dc/existing.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
            {
                "id": 2,
                "file_name": "missing.bin",
                "file_path": "bios/dc/missing.bin",
                "file_size_bytes": 200,
                "md5_hash": "",
            },
        ]

        fw._bios_registry = {
            "platforms": {
                "dc": {
                    "existing.bin": {"description": "Already downloaded", "required": True, "md5": ""},
                    "missing.bin": {"description": "Not yet downloaded", "required": True, "md5": ""},
                },
            },
        }
        fw._bios_files_index = {
            "existing.bin": {"description": "Already downloaded", "required": True, "md5": "", "platform": "dc"},
            "missing.bin": {"description": "Not yet downloaded", "required": True, "md5": "", "platform": "dc"},
        }

        fw._loop = asyncio.get_event_loop()

        download_called_ids = []

        async def fake_download_firmware(fw_id):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw, "download_firmware", side_effect=fake_download_firmware),
            patch.object(fw, "_get_bios_path", return_value=str(bios_dir)),
        ):
            result = await fw.download_required_firmware("dc")

        assert result["success"] is True
        assert result["downloaded"] == 1
        assert 2 in download_called_ids
        assert 1 not in download_called_ids


class TestCheckPlatformBiosOffline:
    """Tests for check_platform_bios registry fallback when RomM is offline."""

    @pytest.mark.asyncio
    async def test_offline_fallback_with_registry(self, plugin, fw, tmp_path):
        """API fails but registry has entries — returns registry-based status."""

        bios_dir = tmp_path / "bios"
        bios_dir.mkdir(parents=True)
        # Create one file present, one missing
        (bios_dir / "scph5501.bin").write_bytes(b"\x00" * 512)

        fw._bios_registry = {
            "platforms": {
                "psx": {
                    "scph5501.bin": {
                        "description": "PS1 US BIOS",
                        "required": True,
                        "firmware_path": "scph5501.bin",
                    },
                    "scph5502.bin": {
                        "description": "PS1 EU BIOS",
                        "required": True,
                        "firmware_path": "scph5502.bin",
                    },
                    "scph1000.bin": {
                        "description": "PS1 JP BIOS",
                        "required": False,
                        "firmware_path": "scph1000.bin",
                    },
                }
            }
        }
        fw._bios_files_index = {}
        for plat, files in fw._bios_registry["platforms"].items():
            for fname, entry in files.items():
                fw._bios_files_index[fname] = {**entry, "platform": plat}

        with (
            patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")),
            patch.object(fw, "_get_bios_path", return_value=str(bios_dir)),
        ):
            result = await fw.check_platform_bios("psx")

        assert result["needs_bios"] is True
        assert result["server_count"] == 3
        assert result["local_count"] == 1
        assert result["required_count"] == 2
        assert result["required_downloaded"] == 1
        assert len(result["files"]) == 3

    @pytest.mark.asyncio
    async def test_offline_no_registry_entries(self, plugin, fw, tmp_path):
        """API fails and no registry entries — returns needs_bios False."""

        fw._bios_registry = {"platforms": {}}
        fw._bios_files_index = {}

        with (
            patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")),
            patch.object(fw, "_get_bios_path", return_value=str(tmp_path / "bios")),
        ):
            result = await fw.check_platform_bios("n64")

        assert result["needs_bios"] is False

    @pytest.mark.asyncio
    async def test_offline_all_required_downloaded(self, plugin, fw, tmp_path):
        """API fails, all required files present — all_downloaded True."""

        bios_dir = tmp_path / "bios"
        dc_dir = bios_dir / "dc"
        dc_dir.mkdir(parents=True)
        (dc_dir / "dc_boot.bin").write_bytes(b"\x00" * 2048)

        fw._bios_registry = {
            "platforms": {
                "dc": {
                    "dc_boot.bin": {
                        "description": "Dreamcast BIOS",
                        "required": True,
                        "firmware_path": "dc/dc_boot.bin",
                    },
                    "dc_flash.bin": {
                        "description": "Dreamcast Flash",
                        "required": False,
                        "firmware_path": "dc/dc_flash.bin",
                    },
                }
            }
        }
        fw._bios_files_index = {}
        for plat, files in fw._bios_registry["platforms"].items():
            for fname, entry in files.items():
                fw._bios_files_index[fname] = {**entry, "platform": plat}

        with (
            patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")),
            patch.object(fw, "_get_bios_path", return_value=str(bios_dir)),
        ):
            result = await fw.check_platform_bios("dc")

        assert result["needs_bios"] is True
        assert result["server_count"] == 2
        assert result["local_count"] == 1
        assert result["required_count"] == 1
        assert result["required_downloaded"] == 1
        # all_downloaded is false because optional file is missing
        assert result["all_downloaded"] is False


class TestPerCoreFiltering:
    """Tests for per-core BIOS filtering in check_platform_bios and _enrich_firmware_file."""

    def test_enrich_uses_core_specific_required(self, fw):
        """When core_so is provided, uses per-core required value."""
        fw._bios_files_index = {
            "gba_bios.bin": {
                "description": "GBA BIOS",
                "required": True,  # OR-logic says required
                "md5": "",
                "platform": "gba",
                "cores": {
                    "mgba_libretro": {"required": False},
                    "gpsp_libretro": {"required": True},
                },
            },
        }
        # mGBA says optional
        file_dict = {"file_name": "gba_bios.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict, core_so="mgba_libretro")
        assert result["required"] is False
        assert result["classification"] == "optional"

    def test_enrich_gpsp_makes_required(self, fw):
        """gpSP core marks gba_bios.bin as required."""
        fw._bios_files_index = {
            "gba_bios.bin": {
                "description": "GBA BIOS",
                "required": True,
                "md5": "",
                "platform": "gba",
                "cores": {
                    "mgba_libretro": {"required": False},
                    "gpsp_libretro": {"required": True},
                },
            },
        }
        file_dict = {"file_name": "gba_bios.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict, core_so="gpsp_libretro")
        assert result["required"] is True
        assert result["classification"] == "required"

    def test_enrich_falls_back_without_core(self, fw):
        """Without core_so, falls back to top-level OR-logic required."""
        fw._bios_files_index = {
            "gba_bios.bin": {
                "description": "GBA BIOS",
                "required": True,
                "md5": "",
                "platform": "gba",
                "cores": {
                    "mgba_libretro": {"required": False},
                    "gpsp_libretro": {"required": True},
                },
            },
        }
        file_dict = {"file_name": "gba_bios.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict, core_so=None)
        assert result["required"] is True  # OR-logic fallback

    def test_enrich_unknown_core_uses_toplevel(self, fw):
        """Core not in cores dict falls back to top-level required."""
        fw._bios_files_index = {
            "gba_bios.bin": {
                "description": "GBA BIOS",
                "required": True,
                "md5": "",
                "platform": "gba",
                "cores": {
                    "mgba_libretro": {"required": False},
                },
            },
        }
        file_dict = {"file_name": "gba_bios.bin", "md5": ""}
        result = fw._enrich_firmware_file(file_dict, core_so="unknown_core_libretro")
        assert result["required"] is True  # top-level OR fallback

    @pytest.mark.asyncio
    async def test_check_platform_bios_filters_by_core(self, fw, tmp_path):
        """check_platform_bios returns all files but marks used_by_active correctly."""
        from unittest.mock import AsyncMock, MagicMock

        firmware_list = [
            {
                "id": 1,
                "file_name": "gba_bios.bin",
                "file_path": "bios/gba/gba_bios.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
            {
                "id": 2,
                "file_name": "gb_bios.bin",
                "file_path": "bios/gba/gb_bios.bin",
                "file_size_bytes": 200,
                "md5_hash": "",
            },
            {
                "id": 3,
                "file_name": "sgb_bios.bin",
                "file_path": "bios/gba/sgb_bios.bin",
                "file_size_bytes": 300,
                "md5_hash": "",
            },
        ]

        fw._bios_registry = {
            "platforms": {
                "gba": {
                    "gba_bios.bin": {
                        "description": "GBA BIOS",
                        "required": True,
                        "firmware_path": "gba_bios.bin",
                        "md5": "",
                        "cores": {"mgba_libretro": {"required": False}, "gpsp_libretro": {"required": True}},
                    },
                    "gb_bios.bin": {
                        "description": "GB BIOS",
                        "required": False,
                        "firmware_path": "gb_bios.bin",
                        "md5": "",
                        "cores": {"gambatte_libretro": {"required": False}, "mgba_libretro": {"required": False}},
                    },
                    "sgb_bios.bin": {
                        "description": "SGB BIOS",
                        "required": False,
                        "firmware_path": "sgb_bios.bin",
                        "md5": "",
                        "cores": {"mgba_libretro": {"required": False}},
                    },
                },
            },
        }
        fw._bios_files_index = {
            "gba_bios.bin": {**fw._bios_registry["platforms"]["gba"]["gba_bios.bin"], "platform": "gba"},
            "gb_bios.bin": {**fw._bios_registry["platforms"]["gba"]["gb_bios.bin"], "platform": "gba"},
            "sgb_bios.bin": {**fw._bios_registry["platforms"]["gba"]["sgb_bios.bin"], "platform": "gba"},
        }

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        # gpSP only uses gba_bios.bin — all files returned but gb/sgb marked as not used by active
        with (
            patch("services.firmware.es_de_config.get_active_core", return_value=("gpsp_libretro", "gpSP")),
            patch("services.firmware.es_de_config.get_available_cores", return_value=[]),
            patch.object(fw, "_get_bios_path", return_value=str(tmp_path / "bios")),
        ):
            result = await fw.check_platform_bios("gba")

        assert result["needs_bios"] is True
        file_names = [f["file_name"] for f in result["files"]]
        assert "gba_bios.bin" in file_names
        assert "gb_bios.bin" in file_names  # present but not used by active
        assert "sgb_bios.bin" in file_names  # present but not used by active
        assert result["server_count"] == 3
        assert result["active_core"] == "gpsp_libretro"
        assert result["active_core_label"] == "gpSP"
        # gpSP requires gba_bios.bin
        gba_file = next(f for f in result["files"] if f["file_name"] == "gba_bios.bin")
        assert gba_file["required"] is True
        assert gba_file["classification"] == "required"
        assert gba_file["used_by_active"] is True
        # gb_bios not used by gpSP
        gb_file = next(f for f in result["files"] if f["file_name"] == "gb_bios.bin")
        assert gb_file["used_by_active"] is False
        assert gb_file["cores"] == {"gambatte_libretro": {"required": False}, "mgba_libretro": {"required": False}}
        # required_count should only count files used by active core
        assert result["required_count"] == 1
        assert result["required_downloaded"] == 0

    @pytest.mark.asyncio
    async def test_check_platform_bios_mgba_all_optional(self, fw, tmp_path):
        """mGBA shows files it uses but all as optional."""
        from unittest.mock import AsyncMock, MagicMock

        firmware_list = [
            {
                "id": 1,
                "file_name": "gba_bios.bin",
                "file_path": "bios/gba/gba_bios.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
            {
                "id": 2,
                "file_name": "gb_bios.bin",
                "file_path": "bios/gba/gb_bios.bin",
                "file_size_bytes": 200,
                "md5_hash": "",
            },
            {
                "id": 3,
                "file_name": "sgb_bios.bin",
                "file_path": "bios/gba/sgb_bios.bin",
                "file_size_bytes": 300,
                "md5_hash": "",
            },
        ]

        fw._bios_registry = {
            "platforms": {
                "gba": {
                    "gba_bios.bin": {
                        "description": "GBA BIOS",
                        "required": True,
                        "firmware_path": "gba_bios.bin",
                        "md5": "",
                        "cores": {"mgba_libretro": {"required": False}, "gpsp_libretro": {"required": True}},
                    },
                    "gb_bios.bin": {
                        "description": "GB BIOS",
                        "required": False,
                        "firmware_path": "gb_bios.bin",
                        "md5": "",
                        "cores": {"mgba_libretro": {"required": False}},
                    },
                    "sgb_bios.bin": {
                        "description": "SGB BIOS",
                        "required": False,
                        "firmware_path": "sgb_bios.bin",
                        "md5": "",
                        "cores": {"mgba_libretro": {"required": False}},
                    },
                },
            },
        }
        fw._bios_files_index = {
            "gba_bios.bin": {**fw._bios_registry["platforms"]["gba"]["gba_bios.bin"], "platform": "gba"},
            "gb_bios.bin": {**fw._bios_registry["platforms"]["gba"]["gb_bios.bin"], "platform": "gba"},
            "sgb_bios.bin": {**fw._bios_registry["platforms"]["gba"]["sgb_bios.bin"], "platform": "gba"},
        }

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        # mGBA uses all 3 files, all optional
        with (
            patch("services.firmware.es_de_config.get_active_core", return_value=("mgba_libretro", "mGBA")),
            patch("services.firmware.es_de_config.get_available_cores", return_value=[]),
        ):
            result = await fw.check_platform_bios("gba")

        assert result["needs_bios"] is True
        assert result["server_count"] == 3
        assert result["required_count"] == 0  # all optional for mGBA
        for f in result["files"]:
            assert f["classification"] == "optional"
            assert f["used_by_active"] is True

    @pytest.mark.asyncio
    async def test_check_platform_bios_no_core_shows_all(self, fw, tmp_path):
        """When core resolution fails, shows all files with OR-logic."""
        from unittest.mock import AsyncMock, MagicMock

        firmware_list = [
            {
                "id": 1,
                "file_name": "gba_bios.bin",
                "file_path": "bios/gba/gba_bios.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]

        fw._bios_registry = {
            "platforms": {
                "gba": {
                    "gba_bios.bin": {
                        "description": "GBA BIOS",
                        "required": True,
                        "firmware_path": "gba_bios.bin",
                        "md5": "",
                        "cores": {"mgba_libretro": {"required": False}},
                    },
                },
            },
        }
        fw._bios_files_index = {
            "gba_bios.bin": {**fw._bios_registry["platforms"]["gba"]["gba_bios.bin"], "platform": "gba"},
        }

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        # Core resolution fails
        with (
            patch("services.firmware.es_de_config.get_active_core", return_value=(None, None)),
            patch("services.firmware.es_de_config.get_available_cores", return_value=[]),
        ):
            result = await fw.check_platform_bios("gba")

        assert result["needs_bios"] is True
        assert result["server_count"] == 1
        assert result["active_core"] is None
        # Falls back to OR-logic: required=True
        assert result["files"][0]["required"] is True

    @pytest.mark.asyncio
    async def test_offline_fallback_includes_all_with_used_by_active(self, plugin, fw, tmp_path):
        """Offline registry fallback returns all files with used_by_active flag."""

        bios_dir = tmp_path / "bios"
        bios_dir.mkdir()
        (bios_dir / "gba_bios.bin").write_bytes(b"\x00" * 100)

        fw._bios_registry = {
            "platforms": {
                "gba": {
                    "gba_bios.bin": {
                        "description": "GBA BIOS",
                        "required": True,
                        "firmware_path": "gba_bios.bin",
                        "md5": "",
                        "cores": {"gpsp_libretro": {"required": True}},
                    },
                    "gb_bios.bin": {
                        "description": "GB BIOS",
                        "required": False,
                        "firmware_path": "gb_bios.bin",
                        "md5": "",
                        "cores": {"mgba_libretro": {"required": False}},
                    },
                },
            },
        }
        fw._bios_files_index = {}

        fw._loop = asyncio.get_event_loop()

        with (
            patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")),
            patch("services.firmware.es_de_config.get_active_core", return_value=("gpsp_libretro", "gpSP")),
            patch("services.firmware.es_de_config.get_available_cores", return_value=[]),
            patch.object(fw, "_get_bios_path", return_value=str(bios_dir)),
        ):
            result = await fw.check_platform_bios("gba")

        assert result["needs_bios"] is True
        file_names = [f["file_name"] for f in result["files"]]
        assert "gba_bios.bin" in file_names
        assert "gb_bios.bin" in file_names  # present but not used by active
        # Check used_by_active flags
        gba_file = next(f for f in result["files"] if f["file_name"] == "gba_bios.bin")
        assert gba_file["used_by_active"] is True
        gb_file = next(f for f in result["files"] if f["file_name"] == "gb_bios.bin")
        assert gb_file["used_by_active"] is False


class TestLoadBiosRegistryErrors:
    """Tests for load_bios_registry error handling."""

    def test_json_parse_error(self, fw, tmp_path):
        """Non-JSON file should log error but not crash."""
        bad_file = tmp_path / "bios_registry.json"
        bad_file.write_text("not valid json {{{")
        fw._plugin_dir = str(tmp_path)
        fw.load_bios_registry()
        assert fw._bios_registry == {}

    def test_file_not_found(self, fw, tmp_path):
        """Missing file should log warning but not crash."""
        fw._plugin_dir = str(tmp_path / "nonexistent")
        fw.load_bios_registry()
        assert fw._bios_registry == {}


class TestFirmwareSlugEdgeCases:
    """Tests for _firmware_slug edge cases."""

    def test_single_part_returns_empty(self, fw):
        assert fw._firmware_slug("bios") == ""

    def test_non_bios_prefix(self, fw):
        assert fw._firmware_slug("firmware/dc/boot.bin") == "firmware"

    def test_empty_path(self, fw):
        assert fw._firmware_slug("") == ""


class TestDownloadFirmwarePostIORegistryHash:
    """Tests for _download_firmware_post_io registry hash verification."""

    def test_verifies_registry_hash_when_no_server_md5(self, fw, tmp_path):
        """Registry hash should be checked even when server md5 is missing."""

        bios_dir = tmp_path / "bios"
        bios_dir.mkdir()
        dest = str(bios_dir / "test.bin")
        tmp_path_file = dest + ".tmp"

        with open(tmp_path_file, "wb") as f:
            f.write(b"test content")

        import hashlib

        expected_md5 = hashlib.md5(b"test content").hexdigest()
        fw._bios_files_index["test.bin"] = {
            "md5": expected_md5,
            "platform": "test",
        }
        fw._state["downloaded_bios"] = {}

        fw_data = {"file_name": "test.bin", "file_path": "bios/test/test.bin", "md5_hash": ""}
        with patch.object(fw, "_get_bios_path", return_value=str(bios_dir)):
            md5_match, reg_hash_valid = fw._download_firmware_post_io(fw_data, 1, dest, tmp_path_file)

        assert md5_match is None
        assert reg_hash_valid is True

    def test_registry_hash_mismatch(self, fw, tmp_path):
        """Registry hash mismatch returns False."""

        bios_dir = tmp_path / "bios"
        bios_dir.mkdir()
        dest = str(bios_dir / "bad.bin")
        tmp_path_file = dest + ".tmp"

        with open(tmp_path_file, "wb") as f:
            f.write(b"bad content")

        fw._bios_files_index["bad.bin"] = {
            "md5": "0000000000000000000000000000dead",
            "platform": "test",
        }
        fw._state["downloaded_bios"] = {}

        fw_data = {"file_name": "bad.bin", "file_path": "bios/test/bad.bin", "md5_hash": ""}
        with patch.object(fw, "_get_bios_path", return_value=str(bios_dir)):
            _md5_match, reg_hash_valid = fw._download_firmware_post_io(fw_data, 2, dest, tmp_path_file)

        assert reg_hash_valid is False


class TestDownloadFirmwareErrors:
    """Tests for download_firmware error handling."""

    @pytest.mark.asyncio
    async def test_fetch_metadata_error(self, fw):
        """Fetch firmware metadata failure returns error."""
        from unittest.mock import AsyncMock, MagicMock

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(side_effect=Exception("not found"))

        result = await fw.download_firmware(999)
        assert result["success"] is False


class TestGetFirmwareStatusOfflineFallback:
    """Tests for get_firmware_status offline fallback to registry."""

    @pytest.mark.asyncio
    async def test_offline_uses_registry(self, fw, plugin, tmp_path):

        fw._bios_registry = {
            "platforms": {
                "dc": {
                    "dc_boot.bin": {
                        "description": "DC BIOS",
                        "required": True,
                        "firmware_path": "dc/dc_boot.bin",
                        "md5": "abc",
                    }
                }
            }
        }
        fw._bios_files_index = {
            "dc_boot.bin": {
                "description": "DC BIOS",
                "required": True,
                "firmware_path": "dc/dc_boot.bin",
                "md5": "abc",
                "platform": "dc",
            }
        }
        plugin._state["shortcut_registry"] = {}

        bios_dir = tmp_path / "bios"
        bios_dir.mkdir()

        fw._loop = asyncio.get_event_loop()

        with (
            patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")),
            patch.object(fw, "_get_bios_path", return_value=str(bios_dir)),
            patch("services.firmware.es_de_config.get_active_core", return_value=(None, None)),
            patch("services.firmware.es_de_config.get_available_cores", return_value=[]),
        ):
            result = await fw.get_firmware_status()

        assert result["success"] is True
        assert result["server_offline"] is True
        assert len(result["platforms"]) == 1
        assert result["platforms"][0]["platform_slug"] == "dc"


# ── Firmware list cache tests ─────────────────────────────


class TestFirmwareListCache:
    """Tests for _get_firmware_list caching behaviour."""

    def _make_service(self, romm_api):
        import decky

        return FirmwareService(
            romm_api=romm_api,
            state={"shortcut_registry": {}, "downloaded_bios": {}},
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            save_state=MagicMock(),
        )

    def test_firmware_list_cached(self):
        """Second call returns cached data without hitting the API again."""
        api = MagicMock()
        api.list_firmware.return_value = [{"id": 1, "file_name": "bios.bin"}]
        fw = self._make_service(api)

        result1 = fw._get_firmware_list()
        result2 = fw._get_firmware_list()

        assert result1 == [{"id": 1, "file_name": "bios.bin"}]
        assert result2 == result1
        assert api.list_firmware.call_count == 1

    def test_firmware_cache_ttl_expired(self):
        """After TTL expires, _get_firmware_list re-fetches from the API."""
        import time

        api = MagicMock()
        api.list_firmware.side_effect = [
            [{"id": 1}],
            [{"id": 1}, {"id": 2}],
        ]
        fw = self._make_service(api)

        result1 = fw._get_firmware_list()
        assert len(result1) == 1
        assert api.list_firmware.call_count == 1

        # Simulate TTL expiry by backdating the cache timestamp
        fw._firmware_cache_at = time.monotonic() - 3601

        result2 = fw._get_firmware_list()
        assert len(result2) == 2
        assert api.list_firmware.call_count == 2

    def test_firmware_cache_invalidate(self):
        """Explicit invalidation triggers a re-fetch on next call."""
        api = MagicMock()
        api.list_firmware.side_effect = [
            [{"id": 1}],
            [{"id": 1}, {"id": 2}],
        ]
        fw = self._make_service(api)

        fw._get_firmware_list()
        assert api.list_firmware.call_count == 1

        fw.invalidate_firmware_cache()
        result = fw._get_firmware_list()
        assert len(result) == 2
        assert api.list_firmware.call_count == 2

    def test_firmware_cache_fallback_on_error(self):
        """HTTP error returns stale cached data instead of raising."""
        api = MagicMock()
        api.list_firmware.side_effect = [
            [{"id": 1, "file_name": "bios.bin"}],
            Exception("connection refused"),
        ]
        fw = self._make_service(api)

        result1 = fw._get_firmware_list()
        assert len(result1) == 1

        # Expire the cache so it tries to re-fetch (must be far enough in the past
        # to exceed TTL even when system uptime is short)
        fw._firmware_cache_at = time.monotonic() - 7200

        result2 = fw._get_firmware_list()
        assert result2 == result1  # Falls back to stale cache
        assert api.list_firmware.call_count == 2

    def test_firmware_cache_error_no_cache_raises(self):
        """HTTP error with no prior cache re-raises so callers can detect offline."""
        api = MagicMock()
        api.list_firmware.side_effect = Exception("connection refused")
        fw = self._make_service(api)

        with pytest.raises(Exception, match="connection refused"):
            fw._get_firmware_list()


class TestCheckPlatformBiosCached:
    """Tests for check_platform_bios_cached — cache-only BIOS status read."""

    def _make_service(self, firmware_cache=None, firmware_cache_at: float = 0, bios_registry=None, state=None):
        import logging

        fw = FirmwareService(
            romm_api=MagicMock(),
            state=state or {"shortcut_registry": {}, "installed_roms": {}, "downloaded_bios": {}},
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            plugin_dir="/fake",
            save_state=MagicMock(),
        )
        fw._firmware_cache = firmware_cache
        fw._firmware_cache_at = firmware_cache_at
        fw._firmware_cache_epoch = firmware_cache_at
        if bios_registry:
            fw._bios_registry = bios_registry
        return fw

    def test_returns_none_when_cache_empty(self):
        """No firmware cache → returns None."""
        fw = self._make_service(firmware_cache=None)
        result = fw.check_platform_bios_cached("gba")
        assert result is None

    def test_returns_needs_bios_false_no_matching_firmware(self):
        """Cache populated but no firmware for this platform → needs_bios=False."""
        fw = self._make_service(
            firmware_cache=[
                {"file_path": "bios/snes/some.bin", "file_name": "some.bin", "file_size_bytes": 100, "md5_hash": ""}
            ],
            firmware_cache_at=1000.0,
        )

        with patch("domain.es_de_config.get_active_core", return_value=(None, None)):
            result = fw.check_platform_bios_cached("gba")

        assert result is not None
        assert result["needs_bios"] is False
        assert result["cached_at"] == 1000.0

    def test_returns_bios_status_from_cache(self, tmp_path):
        """Cache populated with matching firmware → full BIOS status with cached_at."""
        fw = self._make_service(
            firmware_cache=[
                {
                    "file_path": "bios/gba/gba_bios.bin",
                    "file_name": "gba_bios.bin",
                    "file_size_bytes": 16384,
                    "md5_hash": "abc123",
                    "id": 1,
                },
            ],
            firmware_cache_at=42.0,
        )

        with (
            patch("domain.es_de_config.get_active_core", return_value=("mgba_libretro.so", "mGBA")),
            patch(
                "domain.es_de_config.get_available_cores",
                return_value=[{"label": "mGBA", "so": "mgba_libretro.so"}],
            ),
            patch.object(fw, "_get_bios_path", return_value=str(tmp_path)),
        ):
            result = fw.check_platform_bios_cached("gba")

        assert result is not None
        assert result["needs_bios"] is True
        assert result["cached_at"] == 42.0
        assert result["server_count"] == 1
        assert result["local_count"] == 0
        assert result["active_core"] == "mgba_libretro.so"
        assert result["active_core_label"] == "mGBA"
        assert len(result["files"]) == 1
        assert result["files"][0]["file_name"] == "gba_bios.bin"

    def test_does_not_call_http(self):
        """Cache-only method must not invoke any HTTP calls."""
        api = MagicMock()
        import logging

        fw = FirmwareService(
            romm_api=api,
            state={"shortcut_registry": {}, "installed_roms": {}, "downloaded_bios": {}},
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            plugin_dir="/fake",
            save_state=MagicMock(),
        )
        fw._firmware_cache = []
        fw._firmware_cache_at = 1.0
        fw._firmware_cache_epoch = 1.0

        with patch("domain.es_de_config.get_active_core", return_value=(None, None)):
            fw.check_platform_bios_cached("gba")

        api.list_firmware.assert_not_called()
        api.get_firmware.assert_not_called()


class TestFirmwareCachePersistence:
    """Tests for firmware cache disk persistence."""

    def test_cache_loaded_from_disk_on_init(self):
        """Firmware cache restored from disk when data is present."""
        import decky

        cached_items = [{"id": 1, "file_name": "bios.bin", "file_path": "bios/dc/bios.bin"}]
        load_fn = MagicMock(return_value={"items": cached_items, "cached_at": 1000.0})
        fw = FirmwareService(
            romm_api=MagicMock(),
            state={"shortcut_registry": {}, "downloaded_bios": {}},
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            save_state=MagicMock(),
            save_firmware_cache=MagicMock(),
            load_firmware_cache=load_fn,
        )
        assert fw._firmware_cache == cached_items
        assert fw._firmware_cache_epoch == 1000.0
        assert fw._firmware_cache_at > 0
        load_fn.assert_called_once()

    def test_empty_disk_cache_leaves_memory_none(self):
        """Empty disk cache doesn't populate in-memory cache."""
        import decky

        load_fn = MagicMock(return_value={})
        fw = FirmwareService(
            romm_api=MagicMock(),
            state={"shortcut_registry": {}, "downloaded_bios": {}},
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            save_state=MagicMock(),
            save_firmware_cache=MagicMock(),
            load_firmware_cache=load_fn,
        )
        assert fw._firmware_cache is None

    def test_missing_file_handled_gracefully(self):
        """FileNotFoundError from load doesn't crash init."""
        import decky

        load_fn = MagicMock(side_effect=FileNotFoundError("no file"))
        fw = FirmwareService(
            romm_api=MagicMock(),
            state={"shortcut_registry": {}, "downloaded_bios": {}},
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            save_state=MagicMock(),
            save_firmware_cache=MagicMock(),
            load_firmware_cache=load_fn,
        )
        assert fw._firmware_cache is None

    def test_no_load_callback_skips_restore(self):
        """No load_firmware_cache callback skips disk restore gracefully."""
        import decky

        fw = FirmwareService(
            romm_api=MagicMock(),
            state={"shortcut_registry": {}, "downloaded_bios": {}},
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            save_state=MagicMock(),
        )
        assert fw._firmware_cache is None

    def test_cache_persisted_after_http_fetch(self, fw):
        """Firmware cache written to disk after successful HTTP fetch."""
        firmware_list = [{"id": 1, "file_name": "bios.bin", "file_path": "bios/dc/bios.bin"}]
        fw._romm_api.list_firmware.return_value = firmware_list
        fw._firmware_cache = None  # Force refetch

        result = fw._get_firmware_list()

        assert result == firmware_list
        fw._save_firmware_cache.assert_called_once()
        call_data = fw._save_firmware_cache.call_args[0][0]
        assert call_data["items"] == firmware_list
        assert "cached_at" in call_data

    def test_invalidate_clears_persisted_cache(self, fw):
        """invalidate_firmware_cache writes empty dict to disk."""
        fw._firmware_cache = [{"id": 1}]
        fw._firmware_cache_at = 1.0
        fw._firmware_cache_epoch = 1.0

        fw.invalidate_firmware_cache()

        assert fw._firmware_cache is None
        fw._save_firmware_cache.assert_called_once_with({})

    def test_persist_failure_does_not_crash_fetch(self, fw):
        """Disk write failure during fetch doesn't break the return value."""
        firmware_list = [{"id": 1, "file_name": "bios.bin", "file_path": "bios/dc/bios.bin"}]
        fw._romm_api.list_firmware.return_value = firmware_list
        fw._save_firmware_cache.side_effect = OSError("disk full")
        fw._firmware_cache = None

        result = fw._get_firmware_list()

        assert result == firmware_list
        assert fw._firmware_cache == firmware_list
