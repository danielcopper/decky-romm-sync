import asyncio
import os
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from conftest import _make_testable_plugin
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_firmware_file_store import FakeFirmwareFileStore
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.library_peers import FakeArtworkManager
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.firmware_file import FirmwareFileAdapter
from adapters.steam_config import SteamConfigAdapter
from domain.bios import BiosFileEntry
from domain.bios_file import BiosFile
from domain.firmware_cache import FirmwareCacheEntry
from domain.rom import Rom
from services.firmware import FirmwareService, FirmwareServiceConfig
from services.library import LibraryService, LibraryServiceConfig


class FakeSystemResolver:
    """In-memory ``SystemResolver`` for tests.

    Maps known RomM platform slugs to RetroDECK systems and records each
    call. Unknown slugs fall through unchanged, mirroring the real
    resolver's pass-through. Used to assert the core read seams receive a
    normalized system while BIOS-folder lookups stay on the raw slug.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {}
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, platform_slug: str, platform_fs_slug: str | None = None) -> str:
        self.calls.append((platform_slug, platform_fs_slug))
        return self.mapping.get(platform_slug, platform_slug)


def _make_clock() -> FakeClock:
    """Return a fresh FakeClock pinned to a synthetic instant."""
    return FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC))


def _seed_rom(uow: FakeUnitOfWork, *, rom_id: int, platform_slug: str, app_id: int = 1) -> None:
    """Seed one ``Rom`` so firmware's installed-platform read sees the platform."""
    uow.roms.save(
        Rom(
            rom_id=rom_id,
            platform_slug=platform_slug,
            name=f"rom-{rom_id}",
            fs_name=f"rom-{rom_id}.zip",
            shortcut_app_id=app_id,
            last_synced_at="2026-01-01T00:00:00+00:00",
        )
    )


def _seed_firmware_cache(uow: FakeUnitOfWork, entries: list[FirmwareCacheEntry]) -> None:
    """Replace the fake firmware-cache repo's contents in one shot."""
    uow.firmware_cache.replace_all(entries)


def _make_firmware_service(
    *,
    romm_api=None,
    uow_factory: FakeUnitOfWorkFactory | None = None,
    plugin_dir=None,
    clock: FakeClock | None = None,
    firmware_file_store=None,
    retrodeck_paths: FakeRetroDeckPaths | None = None,
    core_info: FakeCoreInfoProvider | None = None,
    resolve_system: FakeSystemResolver | None = None,
    logger=None,
    load_registry: bool = True,
) -> FirmwareService:
    """Build a ``FirmwareService`` over fake adapters + a fake Unit of Work.

    Mirrors the SQLite wiring: persistence flows entirely through
    ``uow_factory`` (no state dict, no persisters). Defaults keep every
    call-site terse; pass overrides only for the axis under test.
    """
    import decky

    fw = FirmwareService(
        config=FirmwareServiceConfig(
            romm_api=romm_api if romm_api is not None else MagicMock(),
            loop=asyncio.get_event_loop(),
            logger=logger if logger is not None else decky.logger,
            plugin_dir=plugin_dir if plugin_dir is not None else decky.DECKY_PLUGIN_DIR,
            clock=clock if clock is not None else _make_clock(),
            firmware_file_store=firmware_file_store if firmware_file_store is not None else FirmwareFileAdapter(),
            retrodeck_paths=retrodeck_paths if retrodeck_paths is not None else FakeRetroDeckPaths(),
            core_info=core_info if core_info is not None else FakeCoreInfoProvider(),
            resolve_system=resolve_system if resolve_system is not None else FakeSystemResolver(),
            uow_factory=uow_factory if uow_factory is not None else FakeUnitOfWorkFactory(),
        ),
    )
    if load_registry:
        fw.load_bios_registry()
    return fw


@pytest.fixture
def plugin():
    p = _make_testable_plugin()
    p.settings = {"romm_url": "", "romm_user": "", "romm_pass": "", "enabled_platforms": {}}
    p._http_adapter = MagicMock()
    p._romm_api = MagicMock()

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    # Shared fake Unit of Work — firmware persistence flows through it, and tests
    # inspect the repos (uow.bios_files / uow.firmware_cache / uow.roms) after the
    # service has run. Exposed on the plugin as ``p._uow`` for assertions.
    p._uow = FakeUnitOfWork()
    p._firmware_service = _make_firmware_service(
        romm_api=p._romm_api,
        uow_factory=FakeUnitOfWorkFactory(p._uow),
        clock=_make_clock(),
    )

    p._sync_service = LibraryService(
        config=LibraryServiceConfig(
            romm_api=p._romm_api,
            steam_config=steam_config,
            settings=p.settings,
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            settings_persister=MagicMock(),
            log_debug=p._log_debug,
            artwork=FakeArtworkManager(),
            uow_factory=FakeUnitOfWorkFactory(),
        ),
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
        fw._retrodeck_paths = FakeRetroDeckPaths(bios=bios)
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
        fw._retrodeck_paths = FakeRetroDeckPaths(bios=bios)
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
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=bios)):
            firmware = {"file_name": "scph5501.bin", "file_path": "bios/ps/scph5501.bin"}
            dest = fw._firmware_dest_path(firmware)
            assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "scph5501.bin")

    def test_uses_dynamic_bios_path(self, fw, tmp_path):
        """Uses ``retrodeck_paths.bios_path()`` for the base directory."""

        sd_bios = "/run/media/deck/Emulation/retrodeck/bios"
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=sd_bios)):
            firmware = {"file_name": "fw.bin", "file_path": "bios/saturn/fw.bin"}
            dest = fw._firmware_dest_path(firmware)
            assert dest == os.path.join(sd_bios, "fw.bin")

    def test_unknown_file_flat_fallback(self, fw, tmp_path):
        """File not in registry falls back to flat in bios root."""

        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=bios)):
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
    async def test_enrich_resolves_system_for_cores_keeps_raw_slug_for_platform(self, tmp_path):
        """Per-platform core reads get the NORMALIZED system; entry slug stays raw.

        ``_enrich_platform_map`` keys ``platform_slug`` / ``has_games`` /
        BIOS-folder file lookups on the raw RomM/BIOS-folder slug (ADR-0010 §4)
        but must feed the resolved RetroDECK system to the ``get_active_core`` /
        ``get_available_cores`` seams (ADR-0010 §2).
        """
        from unittest.mock import AsyncMock, MagicMock

        core_info = FakeCoreInfoProvider(
            active_core=("flycast_libretro.so", "Flycast"),
            available_cores=[{"label": "Flycast", "so": "flycast_libretro.so"}],
        )
        resolver = FakeSystemResolver(mapping={"dc": "dreamcast"})
        fw = _make_firmware_service(core_info=core_info, resolve_system=resolver)

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

        result = await fw.get_firmware_status()

        dc_plat = next(p for p in result["platforms"] if p["platform_slug"] == "dc")
        # Entry identity stays on the RAW slug.
        assert dc_plat["platform_slug"] == "dc"
        # Active-core data resolved under the NORMALIZED system surfaces on the entry.
        assert dc_plat["active_core"] == "flycast_libretro.so"
        assert dc_plat["available_cores"] == [{"label": "Flycast", "so": "flycast_libretro.so"}]
        # Both core read seams received the NORMALIZED system, not the raw slug.
        assert core_info.active_core_calls == [("dreamcast", None)]
        assert core_info.available_cores_calls == ["dreamcast"]
        assert resolver.calls == [("dc", None)]

    @pytest.mark.asyncio
    async def test_has_games_reflects_synced_roms(self, plugin, fw):
        """``has_games`` is True only for platforms with a synced ROM in the registry."""
        firmware_list = [
            {"id": 1, "file_name": "bios_dc.bin", "file_path": "bios/dc/bios_dc.bin", "file_size_bytes": 100},
            {"id": 2, "file_name": "scph.bin", "file_path": "bios/ps2/scph.bin", "file_size_bytes": 200},
        ]
        # A real loop so the executor-run reads hit the shared fake UoW.
        fw._loop = asyncio.get_event_loop()
        # Seed one ROM on "dc" only.
        _seed_rom(plugin._uow, rom_id=42, platform_slug="dc")

        with patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list):
            result = await fw.get_firmware_status()

        dc_plat = next(p for p in result["platforms"] if p["platform_slug"] == "dc")
        ps2_plat = next(p for p in result["platforms"] if p["platform_slug"] == "ps2")
        assert dc_plat["has_games"] is True
        assert ps2_plat["has_games"] is False

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

        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
            result = await fw.get_firmware_status()
        assert result["success"] is True
        assert result["platforms"][0]["files"][0]["downloaded"] is True

    @pytest.mark.asyncio
    async def test_handles_api_error_with_offline_fallback(self, plugin, fw):
        # Real loop: only the HTTP list_firmware fails; the installed-slugs read
        # against the fake UoW still succeeds.
        fw._loop = asyncio.get_event_loop()

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("Connection refused")):
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

        fw._retrodeck_paths = FakeRetroDeckPaths(bios=str(bios_dir))
        fw._loop = asyncio.get_event_loop()

        with (
            patch.object(plugin._romm_api, "get_firmware", return_value=fw_detail),
            patch.object(plugin._romm_api, "download_firmware", side_effect=fake_download),
        ):
            result = await fw.download_firmware(10)

        assert result["success"] is True
        assert result["md5_match"] is True
        assert os.path.exists(result["file_path"])
        # Verify BIOS record persisted via the Unit of Work. n64/bios.bin parses
        # to firmware slug "n64".
        record = plugin._uow.bios_files.get("n64", "bios.bin")
        assert record is not None
        assert record.firmware_id == 10
        assert record.file_path == result["file_path"]
        assert record.platform_slug == "n64"

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
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
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

        # Pre-populate the BIOS registry via the Unit of Work
        plugin._uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug="psx",
                file_name="scph5501.bin",
                file_path=str(bios_file),
                downloaded_at="2026-01-01T00:00:00+00:00",
                firmware_id=42,
            )
        )

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
        # Verify BIOS record removed from the registry
        assert plugin._uow.bios_files.get("psx", "scph5501.bin") is None

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

        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
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


class TestCheckPlatformBiosSlugNormalization:
    """check_platform_bios resolves slug→system for cores, keeps raw slug for BIOS.

    The firmware ``file_path`` and bios registry are keyed on the raw RomM
    platform slug (BIOS-folder vocabulary, ADR-0010 §4). The active-core /
    available-cores reads must instead receive the resolved RetroDECK system.
    """

    @pytest.mark.parametrize(
        ("slug", "system"),
        [
            ("dc", "dreamcast"),
            ("sms", "mastersystem"),
            ("neo-geo-pocket", "ngp"),
            ("gba", "gba"),  # identity: slug already equals system
        ],
    )
    @pytest.mark.asyncio
    async def test_resolves_system_for_cores_keeps_raw_slug_for_bios(self, slug, system):
        from unittest.mock import AsyncMock, MagicMock

        core_info = FakeCoreInfoProvider(
            active_core=("flycast_libretro.so", "Flycast"),
            available_cores=[{"label": "Flycast", "so": "flycast_libretro.so"}],
        )
        resolver = FakeSystemResolver(mapping={"dc": "dreamcast", "sms": "mastersystem", "neo-geo-pocket": "ngp"})
        fw = _make_firmware_service(core_info=core_info, resolve_system=resolver)

        firmware_list = [
            {
                "id": 1,
                "file_name": "boot.bin",
                "file_path": f"bios/{slug}/boot.bin",
                "file_size_bytes": 512,
                "md5_hash": "",
            },
        ]
        fw._bios_registry = {"platforms": {slug: {"boot.bin": {"description": "Boot", "required": True, "md5": ""}}}}
        fw._bios_files_index = {"boot.bin": {"description": "Boot", "required": True, "md5": "", "platform": slug}}

        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        result = await fw.check_platform_bios(slug)

        # RAW slug matched the firmware file_path + registry, so a file is found.
        assert result["needs_bios"] is True
        assert result["server_count"] == 1
        # Both core read seams received the NORMALIZED system.
        assert core_info.active_core_calls == [(system, None)]
        assert core_info.available_cores_calls == [system]
        assert resolver.calls == [(slug, None)]


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
    async def test_resolves_system_for_active_core_keeps_raw_slug_for_filter(self):
        """Active-core read gets the NORMALIZED system; the firmware filter stays raw.

        ``download_required_firmware`` keys the firmware-slug filter on the raw
        RomM/BIOS-folder slug (ADR-0010 §4) but must resolve the slug to a
        RetroDECK system before the ``get_active_core`` read (ADR-0010 §2) so the
        per-core required flags use the correct active core.
        """
        from unittest.mock import AsyncMock, MagicMock

        core_info = FakeCoreInfoProvider(active_core=("flycast_libretro.so", "Flycast"))
        resolver = FakeSystemResolver(mapping={"dc": "dreamcast"})
        fw = _make_firmware_service(core_info=core_info, resolve_system=resolver)

        firmware_list = [
            {
                "id": 1,
                "file_name": "boot.bin",
                "file_path": "bios/dc/boot.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        # Per-core required flag keyed on the active-core .so resolved via the system.
        fw._bios_files_index = {
            "boot.bin": {
                "description": "Boot",
                "required": False,
                "cores": {"flycast_libretro.so": {"required": True}},
                "platform": "dc",
            },
        }
        # run_in_executor returns the firmware list (the only executor call before
        # the batch); download_firmware is awaited directly and is patched below.
        fw._loop = MagicMock()
        fw._loop.run_in_executor = AsyncMock(return_value=firmware_list)

        download_called_ids: list[int] = []

        async def fake_download_firmware(fw_id):
            download_called_ids.append(fw_id)
            return {"success": True}

        with patch.object(fw, "download_firmware", side_effect=fake_download_firmware):
            result = await fw.download_required_firmware("dc")

        # RAW slug matched the firmware file_path filter, so the file is considered.
        # The per-core required flag (keyed on the active core from the NORMALIZED
        # system) marked it required, so it was downloaded.
        assert result["downloaded"] == 1
        assert download_called_ids == [1]
        # get_active_core received the NORMALIZED system, not the raw slug.
        assert core_info.active_core_calls == [("dreamcast", None)]
        assert resolver.calls == [("dc", None)]

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
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
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
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
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
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))),
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
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
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
        fw._core_info.active_core = ("gpsp_libretro", "gpSP")
        fw._core_info.available_cores = []
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))):
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
        fw._core_info.active_core = ("mgba_libretro", "mGBA")
        fw._core_info.available_cores = []
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
        fw._core_info.active_core = (None, None)
        fw._core_info.available_cores = []
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

        fw._core_info.active_core = ("gpsp_libretro", "gpSP")
        fw._core_info.available_cores = []
        with (
            patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")),
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
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


class TestBiosFilesIndexUnloadedRaises:
    """Regression for #348: the property must raise before load_bios_registry()."""

    def test_property_raises_before_load(self):
        """Accessing bios_files_index before load_bios_registry() raises RuntimeError."""
        fw = _make_firmware_service(load_registry=False)

        with pytest.raises(RuntimeError, match="firmware registry not loaded"):
            _ = fw.bios_files_index

    def test_property_returns_dict_after_load(self):
        """After load_bios_registry(), the property returns a dict (possibly empty)."""
        fw = _make_firmware_service(plugin_dir="/nonexistent")

        # FileNotFoundError path still leaves _bios_files_index initialized to {}.
        assert fw.bios_files_index == {}


class TestDownloadFirmwarePostIORegistryHash:
    """Tests for _download_firmware_post_io registry hash verification."""

    def test_verifies_registry_hash_when_no_server_md5(self, plugin, fw, tmp_path):
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

        fw_data = {"file_name": "test.bin", "file_path": "bios/test/test.bin", "md5_hash": ""}
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
            md5_match, reg_hash_valid, error = fw._download_firmware_post_io(fw_data, 1, dest, tmp_path_file)

        assert md5_match is None
        assert reg_hash_valid is True
        assert error is None
        # The BIOS record is persisted via the Unit of Work (firmware slug "test").
        record = plugin._uow.bios_files.get("test", "test.bin")
        assert record is not None
        assert record.firmware_id == 1

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

        fw_data = {"file_name": "bad.bin", "file_path": "bios/test/bad.bin", "md5_hash": ""}
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
            _md5_match, reg_hash_valid, error = fw._download_firmware_post_io(fw_data, 2, dest, tmp_path_file)

        assert reg_hash_valid is False
        assert error is None


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

    @pytest.mark.asyncio
    async def test_malformed_file_path_returns_failure_and_persists_nothing(self, plugin, fw, tmp_path):
        """A firmware whose file_path yields an empty slug fails the BiosFile invariant.

        The service catches the aggregate's ValueError, returns the canonical
        download-failure shape, removes the renamed file, and persists no record
        — no exception escapes.
        """
        content = b"firmware bytes"
        # file_path has a single segment → parse_firmware_slug returns "" →
        # BiosFile.mark_downloaded raises ValueError("platform_slug ... required").
        fw_detail = {
            "id": 7,
            "file_name": "orphan.bin",
            "file_path": "orphan.bin",
            "file_size_bytes": len(content),
            "md5_hash": "",
        }

        bios_dir = tmp_path / "bios"
        bios_dir.mkdir()

        def fake_download(firmware_id, filename, dest):
            with open(dest, "wb") as f:
                f.write(content)

        fw._retrodeck_paths = FakeRetroDeckPaths(bios=str(bios_dir))
        fw._loop = asyncio.get_event_loop()

        with (
            patch.object(plugin._romm_api, "get_firmware", return_value=fw_detail),
            patch.object(plugin._romm_api, "download_firmware", side_effect=fake_download),
        ):
            result = await fw.download_firmware(7)

        # Canonical failure shape, no exception escaped.
        assert result["success"] is False
        assert "error_code" in result
        assert "Invalid firmware metadata" in result["message"]
        # The renamed/downloaded file was cleaned up — nothing left dangling.
        assert not os.path.exists(os.path.join(str(bios_dir), "orphan.bin"))
        # No BiosFile record persisted (empty slug key would be ("", "orphan.bin")).
        assert plugin._uow.bios_files.get("", "orphan.bin") is None
        assert list(plugin._uow.bios_files.iter_all()) == []


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
        # No ROMs seeded in the registry → has_games is False for every platform.

        bios_dir = tmp_path / "bios"
        bios_dir.mkdir()

        fw._loop = asyncio.get_event_loop()

        fw._core_info.active_core = (None, None)
        fw._core_info.available_cores = []
        with (
            patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")),
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.get_firmware_status()

        assert result["success"] is True
        assert result["server_offline"] is True
        assert len(result["platforms"]) == 1
        assert result["platforms"][0]["platform_slug"] == "dc"


# ── Firmware list cache tests ─────────────────────────────


class TestFirmwareListCache:
    """Tests for _get_firmware_list caching behaviour."""

    def _make_service(self, romm_api, uow_factory=None):
        return _make_firmware_service(romm_api=romm_api, uow_factory=uow_factory)

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
        api = MagicMock()
        api.list_firmware.side_effect = [
            [{"id": 1}],
            [{"id": 1}, {"id": 2}],
        ]
        fw = self._make_service(api)

        result1 = fw._get_firmware_list()
        assert len(result1) == 1
        assert api.list_firmware.call_count == 1

        # Simulate TTL expiry by backdating the wall-clock cache epoch
        fw._firmware_cache_epoch = fw._clock.time() - 3601

        result2 = fw._get_firmware_list()
        assert len(result2) == 2
        assert api.list_firmware.call_count == 2

    def test_firmware_cache_ttl_uses_wall_clock_across_restart(self):
        """Cache restored from the DB with stale ``cached_at`` must re-fetch.

        Regression for #344: monotonic-based TTL reset on every plugin
        restart, making a restored cache appear fresh forever.
        """
        clock = _make_clock()
        # Pin the cache epoch two hours before the clock's current wall time —
        # well past _FIRMWARE_CACHE_TTL (1 h).
        stale_epoch = clock.time() - 7200
        uow = FakeUnitOfWork()
        _seed_firmware_cache(
            uow,
            [
                FirmwareCacheEntry.cached(
                    id=1, name="bios.bin", platform_slug="dc", file_size_bytes=2048, cached_at=stale_epoch
                )
            ],
        )

        api = MagicMock()
        api.list_firmware.return_value = [{"id": 2, "file_name": "fresh.bin"}]
        fw = _make_firmware_service(romm_api=api, uow_factory=FakeUnitOfWorkFactory(uow), clock=clock)

        # The restored in-memory cache is reconstructed from the thin aggregate:
        # synthetic file_path round-trips through parse_firmware_slug, md5 dropped.
        assert fw._firmware_cache == [
            {
                "id": 1,
                "file_name": "bios.bin",
                "file_path": "bios/dc/bios.bin",
                "file_size_bytes": 2048,
                "md5_hash": "",
            }
        ]
        assert fw._firmware_cache_epoch == stale_epoch

        result = fw._get_firmware_list()

        assert result == [{"id": 2, "file_name": "fresh.bin"}]
        assert api.list_firmware.call_count == 1

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
        fw._firmware_cache_epoch = fw._clock.time() - 7200

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

    def _make_service(
        self,
        firmware_cache=None,
        firmware_cache_epoch: float = 0,
        bios_registry=None,
        resolve_system=None,
    ) -> tuple[FirmwareService, FakeCoreInfoProvider]:
        import logging

        core_info = FakeCoreInfoProvider()
        fw = _make_firmware_service(
            plugin_dir="/fake",
            logger=logging.getLogger("test"),
            core_info=core_info,
            resolve_system=resolve_system,
        )
        fw._firmware_cache = firmware_cache
        fw._firmware_cache_epoch = firmware_cache_epoch
        if bios_registry:
            fw._bios_registry = bios_registry
        return fw, core_info

    def test_returns_none_when_cache_empty(self):
        """No firmware cache → returns None."""
        fw, _ = self._make_service(firmware_cache=None)
        result = fw.check_platform_bios_cached("gba")
        assert result is None

    def test_returns_needs_bios_false_no_matching_firmware(self):
        """Cache populated but no firmware for this platform → needs_bios=False."""
        fw, core_info = self._make_service(
            firmware_cache=[
                {"file_path": "bios/snes/some.bin", "file_name": "some.bin", "file_size_bytes": 100, "md5_hash": ""}
            ],
            firmware_cache_epoch=1000.0,
        )

        core_info.active_core = (None, None)
        result = fw.check_platform_bios_cached("gba")

        assert result is not None
        assert result["needs_bios"] is False
        assert result["cached_at"] == 1000.0

    def test_returns_bios_status_from_cache(self, tmp_path):
        """Cache populated with matching firmware → full BIOS status with cached_at."""
        fw, core_info = self._make_service(
            firmware_cache=[
                {
                    "file_path": "bios/gba/gba_bios.bin",
                    "file_name": "gba_bios.bin",
                    "file_size_bytes": 16384,
                    "md5_hash": "abc123",
                    "id": 1,
                },
            ],
            firmware_cache_epoch=42.0,
        )

        core_info.active_core = ("mgba_libretro.so", "mGBA")
        core_info.available_cores = [{"label": "mGBA", "so": "mgba_libretro.so"}]
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path))):
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
        import logging

        api = MagicMock()
        core_info = FakeCoreInfoProvider()
        fw = _make_firmware_service(
            romm_api=api,
            plugin_dir="/fake",
            logger=logging.getLogger("test"),
            core_info=core_info,
        )
        fw._firmware_cache = []
        fw._firmware_cache_epoch = 1.0

        core_info.active_core = (None, None)
        fw.check_platform_bios_cached("gba")

        api.list_firmware.assert_not_called()
        api.get_firmware.assert_not_called()

    @pytest.mark.parametrize(
        ("slug", "system"),
        [
            ("dc", "dreamcast"),
            ("sms", "mastersystem"),
            ("neo-geo-pocket", "ngp"),
            ("gba", "gba"),  # identity: slug already equals system
        ],
    )
    def test_resolves_system_for_cores_keeps_raw_slug_for_bios(self, tmp_path, slug, system):
        """Core read seams get the NORMALIZED system; BIOS folder lookup uses RAW slug.

        The firmware cache ``file_path`` and the registry are keyed on the raw
        platform slug (BIOS-folder vocabulary). The active-core / available-cores
        reads must instead receive the resolved RetroDECK system.
        """
        resolver = FakeSystemResolver(mapping={"dc": "dreamcast", "sms": "mastersystem", "neo-geo-pocket": "ngp"})
        fw, core_info = self._make_service(
            firmware_cache=[
                {
                    "file_path": f"bios/{slug}/boot.bin",
                    "file_name": "boot.bin",
                    "file_size_bytes": 512,
                    "md5_hash": "abc",
                    "id": 1,
                },
            ],
            firmware_cache_epoch=7.0,
            bios_registry={"platforms": {slug: {"boot.bin": {"required": True, "md5": "abc"}}}},
            resolve_system=resolver,
        )
        core_info.active_core = ("flycast_libretro.so", "Flycast")
        core_info.available_cores = [{"label": "Flycast", "so": "flycast_libretro.so"}]

        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path))):
            result = fw.check_platform_bios_cached(slug)

        assert result is not None
        # The RAW slug matched the cache + registry, so a file is found.
        assert result["needs_bios"] is True
        assert result["server_count"] == 1
        # Both core read seams received the NORMALIZED system.
        assert core_info.active_core_calls == [(system, None)]
        assert core_info.available_cores_calls == [system]
        assert resolver.calls == [(slug, None)]


class TestFirmwareCachePersistence:
    """Tests for the SQLite firmware-cache round-trip via the Unit of Work."""

    def test_cache_loaded_from_db_on_init(self):
        """Firmware cache restored from the DB when entries are present."""
        uow = FakeUnitOfWork()
        _seed_firmware_cache(
            uow,
            [
                FirmwareCacheEntry.cached(
                    id=1, name="bios.bin", platform_slug="dc", file_size_bytes=512, cached_at=1000.0
                )
            ],
        )

        fw = _make_firmware_service(uow_factory=FakeUnitOfWorkFactory(uow))

        # Reconstructed thin dict: synthetic file_path, md5 dropped.
        assert fw._firmware_cache == [
            {
                "id": 1,
                "file_name": "bios.bin",
                "file_path": "bios/dc/bios.bin",
                "file_size_bytes": 512,
                "md5_hash": "",
            }
        ]
        assert fw._firmware_cache_epoch == 1000.0

    def test_empty_db_cache_leaves_memory_none(self):
        """Empty DB cache doesn't populate the in-memory cache."""
        fw = _make_firmware_service(uow_factory=FakeUnitOfWorkFactory(FakeUnitOfWork()))
        assert fw._firmware_cache is None

    def test_db_read_failure_handled_gracefully(self):
        """A repo error during restore doesn't crash init."""
        uow = FakeUnitOfWork()
        with patch.object(uow.firmware_cache, "iter_all", side_effect=OSError("db locked")):
            fw = _make_firmware_service(uow_factory=FakeUnitOfWorkFactory(uow))
        assert fw._firmware_cache is None

    def test_cache_persisted_after_http_fetch(self, plugin, fw):
        """Firmware cache written to the DB after a successful HTTP fetch."""
        firmware_list = [{"id": 1, "file_name": "bios.bin", "file_path": "bios/dc/bios.bin", "file_size_bytes": 512}]
        fw._romm_api.list_firmware.return_value = firmware_list
        fw._firmware_cache = None  # Force refetch

        result = fw._get_firmware_list()

        assert result == firmware_list
        assert plugin._uow.firmware_cache.replace_count == 1
        # The thin aggregate carries the parsed slug ("dc") and name.
        stored = plugin._uow.firmware_cache.get("dc", "bios.bin")
        assert stored is not None
        assert stored.id == 1
        assert stored.file_size_bytes == 512
        assert stored.cached_at == fw._firmware_cache_epoch

    def test_invalidate_clears_persisted_cache(self, plugin, fw):
        """invalidate_firmware_cache drops every DB cache row."""
        _seed_firmware_cache(
            plugin._uow,
            [FirmwareCacheEntry.cached(id=1, name="x.bin", platform_slug="dc", file_size_bytes=10, cached_at=1.0)],
        )
        fw._firmware_cache = [{"id": 1}]
        fw._firmware_cache_epoch = 1.0

        fw.invalidate_firmware_cache()

        assert fw._firmware_cache is None
        assert list(plugin._uow.firmware_cache.iter_all()) == []

    def test_persist_failure_does_not_crash_fetch(self, plugin, fw):
        """A DB write failure during fetch doesn't break the return value."""
        firmware_list = [{"id": 1, "file_name": "bios.bin", "file_path": "bios/dc/bios.bin", "file_size_bytes": 512}]
        fw._romm_api.list_firmware.return_value = firmware_list
        fw._firmware_cache = None

        with patch.object(plugin._uow.firmware_cache, "replace_all", side_effect=OSError("disk full")):
            result = fw._get_firmware_list()

        assert result == firmware_list
        assert fw._firmware_cache == firmware_list


class TestEnrichFirmwareFileReturnsNewDict:
    """Regression coverage for #170 — _enrich_firmware_file must not mutate its input."""

    def test_input_dict_is_not_mutated(self, fw):
        """Calling _enrich_firmware_file leaves the caller's dict untouched."""
        fw._bios_files_index = {
            "scph5501.bin": {
                "description": "PS1 BIOS",
                "required": True,
                "md5": "abc",
                "platform": "psx",
            },
        }
        original = {"file_name": "scph5501.bin", "md5": "abc"}
        snapshot = dict(original)

        result = fw._enrich_firmware_file(original)

        # Caller's dict still has only the original keys.
        assert original == snapshot
        # Returned dict carries the enrichment.
        assert result is not original
        assert result["required"] is True
        assert result["description"] == "PS1 BIOS"
        assert result["classification"] == "required"
        assert result["hash_valid"] is True

    def test_unknown_file_does_not_mutate_input(self, fw):
        """The unknown-file branch must also return a new dict."""
        fw._bios_files_index = {}
        original = {"file_name": "mystery.bin", "md5": ""}
        snapshot = dict(original)

        result = fw._enrich_firmware_file(original)

        assert original == snapshot
        assert result is not original
        assert result["classification"] == "unknown"
        assert result["required"] is False


class TestDeletePlatformBiosIOLogsWarnings:
    """Coverage for the OSError-warning path in _delete_platform_bios_io."""

    @pytest.mark.asyncio
    async def test_logs_warning_and_collects_error_when_remove_fails(self, plugin, fw, caplog):
        """A per-file OSError surfaces as a logger.warning and an error entry."""
        import logging

        fake_files = FakeFirmwareFileStore()
        fake_files.remove_failures.add("/fake/bios/scph5501.bin")
        fw._firmware_file_store = fake_files

        async def mock_check(slug, rom_filename=None):
            return {
                "needs_bios": True,
                "files": (
                    BiosFileEntry(
                        file_name="scph5501.bin",
                        downloaded=True,
                        local_path="/fake/bios/scph5501.bin",
                        required=True,
                        description="PS1 BIOS",
                        classification="required",
                        cores={},
                        used_by_active=True,
                    ),
                    BiosFileEntry(
                        file_name="scph5502.bin",
                        downloaded=True,
                        local_path="/fake/bios/scph5502.bin",
                        required=True,
                        description="PS1 BIOS (EU)",
                        classification="required",
                        cores={},
                        used_by_active=True,
                    ),
                ),
            }

        fw.check_platform_bios = mock_check
        for name in ("scph5501.bin", "scph5502.bin"):
            plugin._uow.bios_files.save(
                BiosFile.mark_downloaded(
                    platform_slug="psx",
                    file_name=name,
                    file_path=f"/fake/bios/{name}",
                    downloaded_at="2026-01-01T00:00:00+00:00",
                    firmware_id=None,
                )
            )

        with caplog.at_level(logging.WARNING):
            result = await fw.delete_platform_bios("psx")

        # One file deleted (the second), one failed with a logged warning.
        assert result["success"] is False
        assert result["deleted_count"] == 1
        assert any("scph5501.bin" in record.getMessage() for record in caplog.records)
        # The failing file's BIOS record must remain (it wasn't actually removed).
        assert plugin._uow.bios_files.get("psx", "scph5501.bin") is not None
        # The successful file's BIOS record is cleared.
        assert plugin._uow.bios_files.get("psx", "scph5502.bin") is None


class TestBadPathFirmwareCallables:
    """Coverage for the three previously-untested firmware-callable error paths.

    Each test wires a fresh ``FirmwareService`` against the seeded
    ``FakeRommApi`` fixture instead of the plugin's ``MagicMock`` so the
    failure injection runs through the real Protocol surface.
    """

    def _build_service(self, fake_romm_api, *, uow=None):
        """Build a fresh ``FirmwareService`` wired against the supplied fake API."""
        if uow is None:
            uow = FakeUnitOfWork()
        return _make_firmware_service(
            romm_api=fake_romm_api,
            uow_factory=FakeUnitOfWorkFactory(uow),
            firmware_file_store=FakeFirmwareFileStore(),
        )

    def test_invalidate_cache_logs_warning_when_db_clear_fails(self, fake_romm_api, caplog):
        """A DB clear raising ``OSError`` is swallowed with a warning log."""
        import logging

        uow = FakeUnitOfWork()
        fw = self._build_service(fake_romm_api, uow=uow)
        fw._firmware_cache = [{"id": 1, "file_name": "bios.bin"}]
        fw._firmware_cache_epoch = 1.0

        with (
            patch.object(uow.firmware_cache, "clear", side_effect=OSError("disk full")),
            caplog.at_level(logging.WARNING),
        ):
            fw.invalidate_firmware_cache()  # must not raise

        # In-memory cache cleared regardless of DB failure.
        assert fw._firmware_cache is None
        assert fw._firmware_cache_epoch == 0
        # The failure surfaced as a warning.
        assert any("disk full" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_download_all_firmware_returns_error_with_zero_when_list_fetch_fails(self, fake_romm_api, caplog):
        """Initial ``list_firmware`` failure short-circuits with ``downloaded=0``."""
        import logging

        fw = self._build_service(fake_romm_api)
        fw._loop = asyncio.get_event_loop()
        fake_romm_api.fail_on_next(OSError("connection reset"))

        with caplog.at_level(logging.ERROR):
            result = await fw.download_all_firmware("dc")

        assert result["success"] is False
        assert result["downloaded"] == 0
        assert "message" in result
        # The cache was not populated by the failed fetch.
        assert fw._firmware_cache is None

    @pytest.mark.asyncio
    async def test_download_required_firmware_returns_error_with_zero_when_list_fetch_fails(
        self, fake_romm_api, caplog
    ):
        """Initial ``list_firmware`` failure short-circuits with ``downloaded=0``."""
        import logging

        fw = self._build_service(fake_romm_api)
        fw._loop = asyncio.get_event_loop()
        fake_romm_api.fail_on_next(OSError("connection reset"))

        with caplog.at_level(logging.ERROR):
            result = await fw.download_required_firmware("dc")

        assert result["success"] is False
        assert result["downloaded"] == 0
        assert "message" in result
        # The cache was not populated by the failed fetch.
        assert fw._firmware_cache is None
