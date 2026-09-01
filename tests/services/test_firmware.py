import asyncio
import os
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from _factories import _make_testable_plugin
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_core_info_provider import FakeCoreInfoProvider, libretro_option
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_firmware_file_store import FakeFirmwareFileStore
from fakes.fake_firmware_resolver import FakeFirmwareResolver
from fakes.fake_platform_core_reader import FakePlatformCoreReader
from fakes.fake_renderer_gc import FakeRendererGc
from fakes.fake_renderer_rss import FakeRendererRss
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.library_peers import FakeArtworkManager
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.firmware_file import FirmwareFileAdapter
from adapters.steam_config import SteamConfigAdapter
from domain.bios_file import BiosFile
from domain.bios_status import BiosFileEntry
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


def _seed_rom(uow: FakeUnitOfWork, *, rom_id: int, platform_slug: str, app_id: int | None = 1) -> None:
    """Seed one ``Rom`` so firmware's synced-platform read sees the platform.

    ``app_id=None`` seeds an *unbound* ROM (no Steam shortcut) — its platform
    does not count as synced.
    """
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
    clock: FakeClock | None = None,
    firmware_file_store=None,
    firmware_resolver: FakeFirmwareResolver | None = None,
    retrodeck_paths: FakeRetroDeckPaths | None = None,
    core_info: FakeCoreInfoProvider | None = None,
    resolve_system: FakeSystemResolver | None = None,
    platform_core_reader: FakePlatformCoreReader | None = None,
    logger=None,
) -> FirmwareService:
    """Build a ``FirmwareService`` over fake adapters + a fake Unit of Work.

    Mirrors the SQLite wiring: persistence flows entirely through
    ``uow_factory`` (no state dict, no persisters). Defaults keep every
    call-site terse; pass overrides only for the axis under test. The default
    resolver declares nothing and reports every emulator read, so an unseeded
    test sees a machine that genuinely wants no firmware.
    """
    import decky

    return FirmwareService(
        config=FirmwareServiceConfig(
            romm_api=romm_api if romm_api is not None else MagicMock(),
            loop=asyncio.get_event_loop(),
            logger=logger if logger is not None else decky.logger,
            clock=clock if clock is not None else _make_clock(),
            firmware_file_store=firmware_file_store if firmware_file_store is not None else FirmwareFileAdapter(),
            firmware_resolver=firmware_resolver if firmware_resolver is not None else FakeFirmwareResolver(),
            retrodeck_paths=retrodeck_paths if retrodeck_paths is not None else FakeRetroDeckPaths(),
            core_info=core_info if core_info is not None else FakeCoreInfoProvider(),
            resolve_system=resolve_system if resolve_system is not None else FakeSystemResolver(),
            platform_core_reader=platform_core_reader if platform_core_reader is not None else FakePlatformCoreReader(),
            uow_factory=uow_factory if uow_factory is not None else FakeUnitOfWorkFactory(),
        ),
    )


def _inline_executor(fw: FirmwareService) -> None:
    """Run every ``run_in_executor`` hop inline, on the calling task.

    The service offloads several distinct reads — the machine's firmware demand,
    the synced-platform slugs, the RomM listing — and which one a hop is running
    matters. A blanket ``AsyncMock`` answering one canned value for all of them
    would pass whatever the service asked for, so the shim dispatches on the
    function instead of on the call count.
    """

    async def run(_executor, fn, *args):
        return fn(*args)

    loop = MagicMock()
    loop.run_in_executor = run
    fw._loop = loop


def _resolver(fw: FirmwareService) -> FakeFirmwareResolver:
    """The fake resolver behind *fw*, for tests that seed it after construction."""
    resolver = fw._firmware_resolver
    assert isinstance(resolver, FakeFirmwareResolver)
    return resolver


def _stub_listing(fw: FirmwareService, firmware_list: list[dict[str, Any]]) -> None:
    """Answer ``list_firmware`` with *firmware_list* on the service's API stub."""
    api = fw._romm_api
    assert isinstance(api, MagicMock)
    api.list_firmware.return_value = firmware_list


_TEST_CORE = "testcore_libretro"


def _test_core_info() -> FakeCoreInfoProvider:
    """ES-DE offering exactly the one libretro core ``_declare`` attributes its wants to.

    The scope must be non-empty and must contain that core. A platform ES-DE
    offers no libretro core for reads ``unknown`` by design (35 of its 172
    systems are in that position), so a fixture left on the bare
    ``FakeCoreInfoProvider()`` would exercise the no-scope path while its tests
    read as if they were about a complete reading.
    """
    return FakeCoreInfoProvider(options=[libretro_option(_TEST_CORE, "Test Core")])


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
        core_info=_test_core_info(),
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
            active_core=FakeActiveCoreResolver(default=(None, None)),
            disc_resolver=FakeDiscResolver(),
            renderer_rss=FakeRendererRss(),
            renderer_gc=FakeRendererGc(),
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


def _declare(fw: FirmwareService, *specs: tuple[str, str, bool]) -> None:
    """State the machine's demand as ``(file_name, description, required)`` triples.

    Every want is attributed to an owning emulator, because a placement without
    one is exactly what the four-value model removes. These tests leave the
    active core unresolved, where a file required by any emulator counts as
    required for the launch, so the core's name is not the axis under test — the
    tests that DO test per-core filtering name their cores themselves.
    """
    for file_name, description, required in specs:
        _resolver(fw).declare(
            file_name,
            required_by=[_TEST_CORE] if required else [],
            optional_for=[] if required else [_TEST_CORE],
            description=description,
        )


class TestFirmwareDestPath:
    """Where a firmware file lands: the resolver's placement, or flat as a fallback."""

    def test_flat_default_when_nothing_declares_the_file(self, fw, tmp_path):
        """A server file no emulator asks for has no stated layout — flat in the root."""
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        fw._retrodeck_paths = FakeRetroDeckPaths(bios=bios)
        firmware = {"file_name": "bios.bin", "file_path": "bios/n64/bios.bin"}
        dest = fw._firmware_dest_path(firmware, None)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "bios.bin")

    def test_subdirectory_placement_is_honoured(self, fw, tmp_path):
        """A placement below the firmware root places the file in that subdirectory."""
        placement = _resolver(fw).declare(
            "dc_boot.bin", required_by=["flycast_libretro"], relative_path="dc/dc_boot.bin"
        )
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        fw._retrodeck_paths = FakeRetroDeckPaths(bios=bios)
        firmware = {"file_name": "dc_boot.bin", "file_path": "bios/dc/dc_boot.bin"}
        dest = fw._firmware_dest_path(firmware, placement)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "dc", "dc_boot.bin")

    def test_placement_without_a_subdirectory_goes_flat(self, fw, tmp_path):
        placement = _resolver(fw).declare("scph5501.bin", required_by=["mednafen_psx_libretro"])
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=bios)):
            firmware = {"file_name": "scph5501.bin", "file_path": "bios/ps/scph5501.bin"}
            dest = fw._firmware_dest_path(firmware, placement)
            assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "scph5501.bin")

    def test_a_placement_outside_the_root_falls_back_to_the_file_name(self, fw, tmp_path):
        """An emulator keeping firmware in its own tree states no placement here.

        The plugin owns one BIOS directory and writes only inside it, so a
        destination the resolver could not express relative to the firmware root
        leaves the flat default in charge rather than an absolute path from
        outside.
        """
        placement = _resolver(fw).declare("bios7.bin", required_by=["melonds_libretro"], relative_path=None)
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=bios)):
            firmware = {"file_name": "bios7.bin", "file_path": "bios/nds/bios7.bin"}
            dest = fw._firmware_dest_path(firmware, placement)
            assert dest == os.path.join(bios, "bios7.bin")

    def test_uses_dynamic_bios_path(self, fw, tmp_path):
        """Uses ``retrodeck_paths.bios_path()`` for the base directory."""
        sd_bios = "/run/media/deck/Emulation/retrodeck/bios"
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=sd_bios)):
            firmware = {"file_name": "fw.bin", "file_path": "bios/saturn/fw.bin"}
            dest = fw._firmware_dest_path(firmware, None)
            assert dest == os.path.join(sd_bios, "fw.bin")

    def test_a_traversing_placement_is_refused(self, fw, tmp_path):
        """``safe_join`` guards the placement too, not only the server file name."""
        from lib.path_safety import PathTraversalError

        placement = _resolver(fw).declare("evil.bin", required_by=["x_libretro"], relative_path="../evil.bin")
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        with (
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=bios)),
            pytest.raises(PathTraversalError),
        ):
            fw._firmware_dest_path({"file_name": "evil.bin"}, placement)


class TestGetFirmwareStatus:
    @pytest.mark.asyncio
    async def test_returns_grouped_platforms(self, fw, tmp_path):
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

        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

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
        ``get_emulator_options`` seams (ADR-0010 §2).
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        core_info = FakeCoreInfoProvider(
            active_core=("flycast_libretro", "Flycast"),
            options=[libretro_option("flycast_libretro", "Flycast")],
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
        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

        result = await fw.get_firmware_status()

        dc_plat = next(p for p in result["platforms"] if p["platform_slug"] == "dc")
        # Entry identity stays on the RAW slug.
        assert dc_plat["platform_slug"] == "dc"
        # Active-core data resolved under the NORMALIZED system surfaces on the entry.
        assert dc_plat["active_core"] == "flycast_libretro"
        assert dc_plat["emulator_data_available"] is True
        assert dc_plat["emulators"] == [
            {
                "label": "Flycast",
                "kind": "libretro",
                "core_so": "flycast_libretro",
                "is_default": True,
                "bakeable": True,
                "reason": None,
            }
        ]
        # Both core read seams received the NORMALIZED system, not the raw slug.
        assert core_info.active_core_calls == ["dreamcast"]
        assert core_info.emulator_options_calls == ["dreamcast"]
        assert resolver.calls == [("dc", None)]

    async def _psp_active_core_label(self, platform_core_reader):
        """Run ``get_firmware_status`` for a psp platform whose default is a standalone.

        Seeds a standalone-first options list (the PPSSPP flip) so the *default*
        emulator label ("PPSSPP (Standalone)") differs from the libretro
        ``active_core`` label ("PPSSPP") — the exact case the System-page label
        must reflect. Returns the resolved ``active_core_label``.
        """
        from tests.fakes.fake_core_info_provider import libretro_option, standalone_option

        core_info = FakeCoreInfoProvider(
            active_core=("ppsspp_libretro", "PPSSPP"),
            options=[
                standalone_option("%EMULATOR_PPSSPP% -b %ROM%", "PPSSPP (Standalone)"),
                libretro_option("ppsspp_libretro", "PPSSPP"),
            ],
        )
        fw = _make_firmware_service(core_info=core_info, platform_core_reader=platform_core_reader)
        firmware_list = [
            {
                "id": 1,
                "file_name": "ppsspp.bin",
                "file_path": "bios/psp/ppsspp.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        _stub_listing(fw, firmware_list)
        _inline_executor(fw)
        result = await fw.get_firmware_status()
        psp = next(p for p in result["platforms"] if p["platform_slug"] == "psp")
        return psp["active_core_label"]

    @pytest.mark.asyncio
    async def test_active_core_label_is_default_emulator_not_libretro_core(self):
        """No override → the label is the default EMULATOR (standalone), not the libretro core.

        The libretro ``active_core`` stays "PPSSPP" (BIOS filter), but the
        displayed label follows the bakeable default, so a standalone-default
        system reads its standalone label instead of the libretro one (#1210).
        """
        label = await self._psp_active_core_label(FakePlatformCoreReader())
        assert label == "PPSSPP (Standalone)"

    @pytest.mark.asyncio
    async def test_active_core_label_reflects_per_platform_override(self):
        """A per-platform pin surfaces on the System-page label immediately (#1305)."""
        label = await self._psp_active_core_label(FakePlatformCoreReader({"psp": "PPSSPP"}))
        assert label == "PPSSPP"

    @pytest.mark.asyncio
    async def test_active_core_label_degrades_when_override_stale(self):
        """An override that no longer resolves falls back to the default emulator label."""
        label = await self._psp_active_core_label(FakePlatformCoreReader({"psp": "No Longer Here"}))
        assert label == "PPSSPP (Standalone)"

    @pytest.mark.asyncio
    async def test_has_games_reflects_bound_roms(self, plugin, fw):
        """``has_games`` is True only for platforms with a ROM bound to a shortcut.

        - ``dc``: one bound ROM (``shortcut_app_id`` set) -> True.
        - ``ps2``: only an *unbound* ROM (``shortcut_app_id is None``) -> False.
        - ``gba``: no ROM rows at all -> False.
        """
        firmware_list = [
            {"id": 1, "file_name": "bios_dc.bin", "file_path": "bios/dc/bios_dc.bin", "file_size_bytes": 100},
            {"id": 2, "file_name": "scph.bin", "file_path": "bios/ps2/scph.bin", "file_size_bytes": 200},
            {"id": 3, "file_name": "gba_bios.bin", "file_path": "bios/gba/gba_bios.bin", "file_size_bytes": 300},
        ]
        # A real loop so the executor-run reads hit the shared fake UoW.
        fw._loop = asyncio.get_event_loop()
        # "dc": a bound ROM. "ps2": only an unbound ROM. "gba": no ROM rows.
        _seed_rom(plugin._uow, rom_id=42, platform_slug="dc", app_id=1)
        _seed_rom(plugin._uow, rom_id=43, platform_slug="ps2", app_id=None)

        with patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list):
            result = await fw.get_firmware_status()

        dc_plat = next(p for p in result["platforms"] if p["platform_slug"] == "dc")
        ps2_plat = next(p for p in result["platforms"] if p["platform_slug"] == "ps2")
        gba_plat = next(p for p in result["platforms"] if p["platform_slug"] == "gba")
        assert dc_plat["has_games"] is True
        assert ps2_plat["has_games"] is False
        assert gba_plat["has_games"] is False

    @pytest.mark.asyncio
    async def test_detects_downloaded_files(self, fw, tmp_path):
        # File goes flat in bios root — nothing declares a placement for it
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

        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

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


_DC_CORE = "flycast_libretro"


def _dc_resolver() -> FakeFirmwareResolver:
    """Two files the dc core will not run without, and one it merely accepts."""
    resolver = FakeFirmwareResolver()
    resolver.declare("req1.bin", required_by=[_DC_CORE], description="Required BIOS 1")
    resolver.declare("req2.bin", required_by=[_DC_CORE], description="Required BIOS 2")
    resolver.declare("opt1.bin", optional_for=[_DC_CORE], description="Optional firmware")
    return resolver


def _dc_core_info() -> FakeCoreInfoProvider:
    """A dc platform whose launching core is the one declaring those files."""
    from tests.fakes.fake_core_info_provider import libretro_option

    return FakeCoreInfoProvider(
        active_core=(_DC_CORE, "Flycast"),
        options=[libretro_option(_DC_CORE, "Flycast")],
    )


class TestGetFirmwareStatusBiosAggregates:
    """``get_firmware_status`` ships per-platform BIOS aggregates + ``bios_level``.

    The System page reads the unknown/ok/partial/missing decision and display
    counts off this payload instead of re-deriving the threshold logic in the
    frontend (#461). The level is computed by the same
    ``domain.bios_status.compute_bios_level`` the game-detail path uses, from the
    same classified files.
    """

    @staticmethod
    def _firmware(*names: str) -> list[dict[str, Any]]:
        return [
            {
                "id": i + 1,
                "file_name": name,
                "file_path": f"bios/dc/{name}",
                "file_size_bytes": 100,
                "md5_hash": "",
            }
            for i, name in enumerate(names)
        ]

    async def _run(self, tmp_path, firmware_list, downloaded: set[str]):
        """Run get_firmware_status against the dc demand and the given downloads."""
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True, exist_ok=True)
        for name in downloaded:
            (bios_dir / name).write_bytes(b"\x00" * 100)

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = firmware_list
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=_dc_resolver(),
            core_info=_dc_core_info(),
            retrodeck_paths=FakeRetroDeckPaths(bios=str(bios_dir)),
        )
        _inline_executor(fw)

        result = await fw.get_firmware_status()
        return next(p for p in result["platforms"] if p["platform_slug"] == "dc")

    @pytest.mark.asyncio
    async def test_all_required_ready_is_ok(self, tmp_path):
        """All required files downloaded → bios_level 'ok' + matching counts."""
        plat = await self._run(
            tmp_path, self._firmware("req1.bin", "req2.bin", "opt1.bin"), downloaded={"req1.bin", "req2.bin"}
        )
        assert plat["bios_level"] == "ok"
        assert plat["required_count"] == 2
        assert plat["required_downloaded"] == 2
        assert plat["server_count"] == 3
        assert plat["local_count"] == 2

    @pytest.mark.asyncio
    async def test_some_required_downloaded_is_partial(self, tmp_path):
        """One of two required files present → bios_level 'partial'."""
        plat = await self._run(tmp_path, self._firmware("req1.bin", "req2.bin", "opt1.bin"), downloaded={"req1.bin"})
        assert plat["bios_level"] == "partial"
        assert plat["required_count"] == 2
        assert plat["required_downloaded"] == 1

    @pytest.mark.asyncio
    async def test_no_required_downloaded_is_missing(self, tmp_path):
        """No required file present → bios_level 'missing'."""
        plat = await self._run(tmp_path, self._firmware("req1.bin", "req2.bin", "opt1.bin"), downloaded=set())
        assert plat["bios_level"] == "missing"
        assert plat["required_count"] == 2
        assert plat["required_downloaded"] == 0
        assert plat["local_count"] == 0

    @pytest.mark.asyncio
    async def test_a_wanted_file_the_library_lacks_is_still_a_row(self, tmp_path):
        """The third row kind: wanted, missing, and not downloadable from here.

        The listing carries only the optional file; the two the dc core will not
        run without are wanted all the same, so they are shown — marked
        ``on_server`` false, which is the field the page's buttons and totals
        filter on, and carrying no server id because there is no server record.
        """
        plat = await self._run(tmp_path, self._firmware("opt1.bin"), downloaded={"opt1.bin"})

        rows = {f["file_name"]: f for f in plat["files"]}
        assert set(rows) == {"opt1.bin", "req1.bin", "req2.bin"}
        assert rows["opt1.bin"]["on_server"] is True
        for name in ("req1.bin", "req2.bin"):
            assert rows[name]["on_server"] is False
            assert rows[name]["id"] is None
            assert rows[name]["wanted"] == "needed"

        # They are missing prerequisites, so the platform is not ready.
        assert plat["required_count"] == 2
        assert plat["required_downloaded"] == 0
        assert plat["bios_level"] == "missing"

    @pytest.mark.asyncio
    async def test_unanswerable_platform_projects_unknown_level(self, tmp_path):
        """System-page projection: server files nothing could answer for → 'unknown'.

        A platform one of whose emulators could not be read classifies every
        unmatched file ``unknown`` (known_count 0), so the aggregate stamps
        ``bios_level == "unknown"`` instead of the false ``"ok"`` (#1520). It is
        never flagged as a BIOS-needed platform (required_count is 0).
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {
                "id": 1,
                "file_name": "vita.bin",
                "file_path": "bios/psvita/vita.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=FakeFirmwareResolver(unread_cores=frozenset({"vita_libretro"})),
            core_info=FakeCoreInfoProvider(options=[libretro_option("vita_libretro", "Vita")]),
        )
        _inline_executor(fw)

        result = await fw.get_firmware_status()

        plat = next(p for p in result["platforms"] if p["platform_slug"] == "psvita")
        assert plat["bios_level"] == "unknown"
        assert plat["required_count"] == 0
        assert plat["server_count"] == 1

    @pytest.mark.asyncio
    async def test_platform_whose_emulators_were_all_read_is_not_unknown(self, tmp_path):
        """Every emulator read and none asks for the file → an answer, not a gap.

        The counterpart to the case above, and the whole point of the four-value
        model: the same empty match set means "nothing needs this" here and
        "nothing could be established" there.
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {
                "id": 1,
                "file_name": "stray.bin",
                "file_path": "bios/snes/stray.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=FakeFirmwareResolver(),
            core_info=FakeCoreInfoProvider(options=[libretro_option("snes9x_libretro", "Snes9x")]),
        )
        _inline_executor(fw)

        result = await fw.get_firmware_status()

        plat = next(p for p in result["platforms"] if p["platform_slug"] == "snes")
        assert [f["wanted"] for f in plat["files"]] == ["not_needed"]
        assert plat["bios_level"] == "ok"

    @pytest.mark.asyncio
    async def test_the_library_ratio_counts_only_what_the_library_holds(self, tmp_path):
        """ "N of M files" is a progress bar over a set the user can complete.

        Optional files no library holds are shown as rows but stay out of the
        ratio — folding them in reports work outstanding on a system that needs
        nothing. Measured: a stock RetroDECK's SNES emulators declare 26 optional
        files, so the header would have read "0 / 26 files, 26 missing" for a
        system no core requires anything from.
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {"id": 1, "file_name": "held.bin", "file_path": "bios/snes/held.bin", "file_size_bytes": 1, "md5_hash": ""},
        ]
        resolver = FakeFirmwareResolver()
        resolver.declare("held.bin", optional_for=["snes9x_libretro"])
        for name in ("absent1.bin", "absent2.bin"):
            resolver.declare(name, optional_for=["snes9x_libretro"])
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=resolver,
            core_info=FakeCoreInfoProvider(options=[libretro_option("snes9x_libretro", "Snes9x")]),
        )
        _inline_executor(fw)

        result = await fw.get_firmware_status()
        plat = next(p for p in result["platforms"] if p["platform_slug"] == "snes")

        # All three are listed — the machine wants them and the user should see so.
        assert len(plat["files"]) == 3
        # Only the one the library holds is in the ratio.
        assert plat["server_count"] == 1
        assert plat["local_count"] == 0
        # None is required, so the badge stays quiet either way.
        assert plat["required_count"] == 0

    @pytest.mark.asyncio
    async def test_a_required_file_the_library_lacks_still_counts_as_required(self, tmp_path):
        """The other side of the same split: readiness is not a progress bar.

        A required file nobody can fetch is still a prerequisite, so it raises
        ``required_count`` and holds the level down — the download affordance is
        what withholds itself, not the count.
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = []
        resolver = FakeFirmwareResolver()
        resolver.declare("lynxboot.img", required_by=["handy_libretro"], description="Boot ROM")
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=resolver,
            core_info=FakeCoreInfoProvider(
                active_core=("handy_libretro", "Handy"),
                options=[libretro_option("handy_libretro", "Handy")],
            ),
        )
        _inline_executor(fw)

        result = await fw.check_platform_bios("atarilynx")

        assert result["required_count"] == 1
        assert result["required_downloaded"] == 0
        assert result["bios_level"] == "missing"
        assert result["server_count"] == 0
        assert result["files"][0]["on_server"] is False

    @pytest.mark.asyncio
    async def test_a_file_the_library_files_under_another_platform_is_not_called_absent(self, tmp_path):
        """ "Not in your library" is a claim about the library, not about a directory.

        A core that serves several systems declares the same file for each of
        them while RomM files it under one directory. Checking only this
        platform's slice of the listing would tell the user to upload a file they
        already have — and it is one download either way, because the destination
        comes from the placement rather than from the directory it was listed
        under.
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {
                "id": 1,
                "file_name": "bios.gg",
                "file_path": "bios/gamegear/bios.gg",
                "file_size_bytes": 1,
                "md5_hash": "",
            },
        ]
        resolver = FakeFirmwareResolver()
        resolver.declare("bios.gg", optional_for=["genesis_plus_gx_libretro"], description="Game Gear BIOS")
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=resolver,
            core_info=FakeCoreInfoProvider(options=[libretro_option("genesis_plus_gx_libretro", "Genesis Plus GX")]),
        )
        _inline_executor(fw)

        result = await fw.get_firmware_status()

        # The listing names one platform, so only that one carries the row —
        # and it carries it as a library file, not as an absent one.
        assert [p["platform_slug"] for p in result["platforms"]] == ["gamegear"]
        assert [f["on_server"] for f in result["platforms"][0]["files"]] == [True]

    @pytest.mark.asyncio
    async def test_the_machine_is_asked_once_for_the_whole_overview(self, tmp_path):
        """One whole-machine question per call, not one per platform.

        On a real device the resolver walks a few hundred ``.info`` files per
        query and memoises nothing, so a per-platform loop would multiply a
        hundreds-of-milliseconds read by the platform count.
        """
        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {"id": 1, "file_name": "a.bin", "file_path": "bios/dc/a.bin", "file_size_bytes": 1, "md5_hash": ""},
            {"id": 2, "file_name": "b.bin", "file_path": "bios/psx/b.bin", "file_size_bytes": 1, "md5_hash": ""},
            {"id": 3, "file_name": "c.bin", "file_path": "bios/gba/c.bin", "file_size_bytes": 1, "md5_hash": ""},
        ]
        resolver = FakeFirmwareResolver()
        fw = _make_firmware_service(romm_api=romm_api, firmware_resolver=resolver)
        _inline_executor(fw)

        result = await fw.get_firmware_status()

        assert len(result["platforms"]) == 3
        assert resolver.calls == 1

    @pytest.mark.asyncio
    async def test_server_offline_still_answers_readiness(self, plugin, tmp_path):
        """Readiness needs no server: ES-DE, the resolver and the disk answer it.

        What an unreachable RomM costs is the files only it knows about and the
        ability to download — not the requirement, and not the platform.
        """
        _seed_rom(plugin._uow, rom_id=42, platform_slug="dc", app_id=1)
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True, exist_ok=True)
        (bios_dir / "req1.bin").write_bytes(b"\x00" * 100)

        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_resolver=_dc_resolver(),
            core_info=_dc_core_info(),
            retrodeck_paths=FakeRetroDeckPaths(bios=str(bios_dir)),
        )
        fw._loop = asyncio.get_event_loop()

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")):
            result = await fw.get_firmware_status()

        assert result["server_offline"] is True
        dc = next(p for p in result["platforms"] if p["platform_slug"] == "dc")
        assert dc["has_games"] is True
        assert {f["file_name"] for f in dc["files"]} == {"req1.bin", "req2.bin", "opt1.bin"}
        assert all(f["on_server"] is False for f in dc["files"])
        # One of the two required files is on disk — a real, partial answer.
        assert dc["required_count"] == 2
        assert dc["required_downloaded"] == 1
        assert dc["bios_level"] == "partial"

    @pytest.mark.asyncio
    async def test_a_synced_platform_nothing_wants_stays_off_the_page(self, plugin, tmp_path):
        """Seeding every synced platform would fill the page with 0/0 rows."""
        _seed_rom(plugin._uow, rom_id=44, platform_slug="nes", app_id=1)
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_resolver=FakeFirmwareResolver(),
        )
        fw._loop = asyncio.get_event_loop()

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")):
            result = await fw.get_firmware_status()

        assert result["platforms"] == []

    @pytest.mark.asyncio
    async def test_no_exists_read_escapes_the_bios_directory(self, plugin, fw, tmp_path):
        """#966 NIT2: a server-supplied traversal name is skipped, not joined.

        The listing is server-controlled, so a ``file_name`` carrying ``..`` must
        be dropped (log-and-skip) rather than steering an ``exists()`` read
        outside the BIOS sandbox.
        """
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True, exist_ok=True)
        (bios_dir / "good.bin").write_bytes(b"\x00" * 100)
        (tmp_path / "retrodeck" / "evil.bin").write_bytes(b"\x00" * 100)

        firmware_list = [
            {"id": 1, "file_name": "good.bin", "file_path": "bios/dc/good.bin", "file_size_bytes": 1, "md5_hash": ""},
            {
                "id": 2,
                "file_name": "../evil.bin",
                "file_path": "bios/dc/../evil.bin",
                "file_size_bytes": 1,
                "md5_hash": "",
            },
        ]
        fw._loop = asyncio.get_event_loop()

        real_exists = fw._firmware_file_store.exists
        checked: list[str] = []

        def _tracking_exists(path):
            checked.append(path)
            return real_exists(path)

        fw._firmware_file_store.exists = _tracking_exists

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.get_firmware_status()

        plat = next(p for p in result["platforms"] if p["platform_slug"] == "dc")
        assert {f["file_name"] for f in plat["files"]} == {"good.bin"}
        real_bios = os.path.realpath(str(bios_dir))
        for path in checked:
            assert os.path.realpath(path).startswith(real_bios + os.sep)


class TestCheckPlatformBiosUnknown:
    """``check_platform_bios`` surfaces the 'unknown' state for unanswerable platforms.

    A platform one of whose emulators could not be read has every unmatched file
    classified ``unknown`` (known_count 0), so the per-game BIOS payload ships
    ``bios_level == "unknown"`` instead of a false ``"ok"`` all-clear (#1520).
    """

    @pytest.mark.asyncio
    async def test_unanswerable_platform_is_unknown(self, tmp_path):
        from tests.fakes.fake_core_info_provider import libretro_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {
                "id": 1,
                "file_name": "vita.bin",
                "file_path": "bios/psvita/vita.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=FakeFirmwareResolver(unread_cores=frozenset({"vita_libretro"})),
            core_info=FakeCoreInfoProvider(options=[libretro_option("vita_libretro", "Vita")]),
        )
        _inline_executor(fw)

        result = await fw.check_platform_bios("psvita")

        assert result["needs_bios"] is True
        assert result["server_count"] == 1
        assert result["known_count"] == 0
        assert result["unknown_count"] == 1
        assert result["required_count"] == 0
        assert result["bios_level"] == "unknown"

    @pytest.mark.asyncio
    async def test_an_unread_core_outside_the_platform_does_not_make_it_unknown(self, tmp_path):
        """The doubt is scoped to the emulators THIS platform offers.

        Measured on a stock RetroDECK (211 cores, 172 ES-DE systems): five
        installed cores ship without a ``.info``, and exactly one of them —
        ``amiarcadia_libretro`` — is offered by any system. Letting an unread
        core silence platforms that do not offer it would make ``not_needed``
        unreachable and put the four-value model back at three.
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {
                "id": 1,
                "file_name": "stray.bin",
                "file_path": "bios/snes/stray.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=FakeFirmwareResolver(unread_cores=frozenset({"amiarcadia_libretro"})),
            core_info=FakeCoreInfoProvider(options=[libretro_option("snes9x_libretro", "Snes9x")]),
        )
        _inline_executor(fw)

        result = await fw.check_platform_bios("snes")

        assert result["unknown_count"] == 0
        assert [f["wanted"] for f in result["files"]] == ["not_needed"]

    @pytest.mark.asyncio
    async def test_the_platform_that_does_offer_the_unread_core_is_unknown(self, tmp_path):
        """The other half of the same machine, and the reason the value exists.

        ``arcadia`` (Emerson Arcadia 2001) is the one system of 172 that offers
        ``amiarcadia_libretro``, whose ``.info`` RetroDECK does not ship. It is
        the whole population of ``unknown`` on a stock install — rare, and
        reachable, which is what the fourth value is for.
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {
                "id": 1,
                "file_name": "stray.bin",
                "file_path": "bios/arcadia/stray.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=FakeFirmwareResolver(unread_cores=frozenset({"amiarcadia_libretro"})),
            core_info=FakeCoreInfoProvider(options=[libretro_option("amiarcadia_libretro", "Amiarcadia")]),
        )
        _inline_executor(fw)

        result = await fw.check_platform_bios("arcadia")

        assert result["unknown_count"] == 1
        assert [f["wanted"] for f in result["files"]] == ["unknown"]
        assert result["bios_level"] == "unknown"

    @pytest.mark.asyncio
    async def test_a_row_the_library_does_not_hold_cannot_cancel_the_unknown_verdict(self, tmp_path):
        """``known_count`` is about server files, so a union row must not raise it.

        A beyond-server row exists only because an emulator declared the file,
        so it is always ``needed``/``optional`` — always "known". Counted, one of
        them cancels ``unknown`` for a platform whose every server file went
        unanswered: the headline goes green while the row underneath still says
        nothing could answer for it.
        """
        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {"id": 1, "file_name": "stray.bin", "file_path": "bios/snes/stray.bin", "file_size_bytes": 100},
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=FakeFirmwareResolver(unread_cores=frozenset({"snes9x_libretro"})),
            core_info=FakeCoreInfoProvider(
                options=[libretro_option("snes9x_libretro", "Snes9x"), libretro_option("bsnes_libretro", "bsnes")]
            ),
            retrodeck_paths=FakeRetroDeckPaths(bios=str(tmp_path / "bios")),
        )
        # Declared by the core that WAS read, and absent from the library — the
        # union row. The server's own file stays unanswerable either way.
        _resolver(fw).declare("extra.bin", required_by=["bsnes_libretro"])
        _inline_executor(fw)

        result = await fw.check_platform_bios("snes")

        wanted = {f["file_name"]: f["wanted"] for f in result["files"]}
        assert wanted == {"stray.bin": "unknown", "extra.bin": "needed"}
        assert result["server_count"] == 1
        assert result["known_count"] == 0
        assert result["unknown_count"] == 1
        assert result["bios_level"] == "unknown"

    @pytest.mark.asyncio
    async def test_an_unreadable_emulator_list_leaves_the_scope_unestablished(self, tmp_path):
        """No ``es_systems.xml`` → the scope itself is unknown, so nothing is ruled out."""
        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {
                "id": 1,
                "file_name": "stray.bin",
                "file_path": "bios/snes/stray.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=FakeFirmwareResolver(),
            core_info=FakeCoreInfoProvider(available=False),
        )
        _inline_executor(fw)

        result = await fw.check_platform_bios("snes")

        assert result["unknown_count"] == 1
        assert result["bios_level"] == "unknown"

    @pytest.mark.asyncio
    async def test_a_resolver_that_could_not_answer_never_reads_as_needing_none(self, tmp_path):
        """The adapter's failure shape must not clear a real requirement."""
        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {
                "id": 1,
                "file_name": "stray.bin",
                "file_path": "bios/snes/stray.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=FakeFirmwareResolver(resolved=False),
        )
        _inline_executor(fw)

        result = await fw.check_platform_bios("snes")

        assert result["bios_level"] == "unknown"
        assert result["known_count"] == 0

    @pytest.mark.asyncio
    async def test_answered_platform_threads_known_count_and_is_not_unknown(self, tmp_path):
        """A platform with declared files ships known_count > 0 and keeps its level."""
        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {"id": 1, "file_name": "req1.bin", "file_path": "bios/dc/req1.bin", "file_size_bytes": 100, "md5_hash": ""},
            {"id": 2, "file_name": "opt1.bin", "file_path": "bios/dc/opt1.bin", "file_size_bytes": 100, "md5_hash": ""},
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=_dc_resolver(),
            core_info=_dc_core_info(),
        )
        _inline_executor(fw)

        result = await fw.check_platform_bios("dc")

        # Three declared files, but only the two the listing carries count:
        # ``known_count`` is weighed against ``server_count``, so the row the
        # library does not hold is not in its set.
        assert result["known_count"] == 2
        assert result["server_count"] == 2
        assert result["bios_level"] != "unknown"


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
        assert "reason" in result

    @pytest.mark.asyncio
    async def test_rejects_traversal_in_server_file_name(self, plugin, fw, tmp_path):
        """#966: a server ``file_name`` of ``../evil.desktop`` is rejected, nothing written outside BIOS."""
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        # The traversal target lives a sibling of the bios dir.
        escape_target = tmp_path / "retrodeck" / "evil.desktop"

        fw_detail = {
            "id": 10,
            "file_name": "../evil.desktop",
            "file_path": "bios/n64/evil.desktop",
            "file_size_bytes": 10,
            "md5_hash": "",
        }

        fw._retrodeck_paths = FakeRetroDeckPaths(bios=str(bios_dir))
        fw._loop = asyncio.get_event_loop()

        download_called = []

        def fake_download(firmware_id, filename, dest):
            download_called.append(dest)
            with open(dest, "wb") as f:
                f.write(b"evil")

        with (
            patch.object(plugin._romm_api, "get_firmware", return_value=fw_detail),
            patch.object(plugin._romm_api, "download_firmware", side_effect=fake_download),
        ):
            result = await fw.download_firmware(10)

        # Canonical path_traversal failure shape.
        assert result["success"] is False
        assert result["reason"] == "path_traversal"
        assert "message" in result
        # No download was ever attempted (rejected before make_dirs / fetch).
        assert download_called == []
        # Nothing written outside the BIOS directory.
        assert not escape_target.exists()
        # No BIOS record persisted.
        assert plugin._uow.bios_files.get("n64", "../evil.desktop") is None


class TestDownloadAllFirmware:
    @pytest.mark.asyncio
    async def test_downloads_missing_only(self, plugin, fw, tmp_path):

        # Pre-create one file so it's skipped (flat in bios root — no declared placement)
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

        async def fake_download_firmware(fw_id, _placements):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw, "_download_one", side_effect=fake_download_firmware),
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
        """Deleting platform BIOS removes downloaded files and state entries.

        ``check_platform_bios`` returns its ``files`` as ``asdict`` dicts
        (``[asdict(f) for f in files]``), so the mock mirrors that shape — not
        bare ``BiosFileEntry`` objects. Driving the real output shape is what
        guards the delete path against the #750 dict/attribute mismatch.
        """
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

        # Mock check_platform_bios with the REAL output shape: asdict dicts.
        async def mock_check(slug, active_core_so=None):
            return {
                "needs_bios": True,
                "server_count": 1,
                "local_count": 1,
                "all_downloaded": True,
                "files": [
                    asdict(
                        BiosFileEntry(
                            file_name="scph5501.bin",
                            downloaded=True,
                            local_path=str(bios_file),
                            description="PS1 BIOS",
                            wanted="needed",
                            required_by_active=True,
                            cores={},
                            used_by_active=True,
                        )
                    ),
                ],
            }

        fw.check_platform_bios = mock_check

        result = await fw.delete_platform_bios("psx")
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not bios_file.exists()
        # Verify BIOS record removed from the registry
        assert plugin._uow.bios_files.get("psx", "scph5501.bin") is None

    @pytest.mark.asyncio
    async def test_delete_platform_bios_real_check_output_shape(self, plugin, tmp_path):
        """Regression for #750: delete works against the real asdict dict shape.

        Drives ``delete_platform_bios`` end-to-end through the *real*
        ``check_platform_bios`` (server-offline registry fallback), so the
        ``files`` list is the genuine ``[asdict(f) for f in files]`` payload
        the callable hands to ``_delete_platform_bios_io``. Before the fix that
        worker read ``f.downloaded`` / ``f.local_path`` / ``f.file_name`` as
        attributes on those dicts, raising ``AttributeError`` in the executor
        and deleting nothing — the "Failed to delete BIOS files" the modal showed.
        """
        bios_dir = tmp_path / "bios"
        bios_dir.mkdir(parents=True)
        # One downloaded file (store sees it) + one never-downloaded.
        store = FakeFirmwareFileStore({str(bios_dir / "scph5501.bin"): b"\x00" * 512})

        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_file_store=store,
            retrodeck_paths=FakeRetroDeckPaths(bios=str(bios_dir)),
        )
        fw._loop = asyncio.get_event_loop()
        _declare(fw, ("scph5501.bin", "PS1 US BIOS", True), ("scph5502.bin", "PS1 EU BIOS", True))

        # The downloaded file has a BiosFile record to prune (firmware slug "ps").
        plugin._uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug="ps",
                file_name="scph5501.bin",
                file_path=str(bios_dir / "scph5501.bin"),
                downloaded_at="2026-01-01T00:00:00+00:00",
                firmware_id=42,
            )
        )

        firmware_list = [
            {
                "id": 1,
                "file_name": "scph5501.bin",
                "file_path": "bios/ps/scph5501.bin",
                "file_size_bytes": 512,
                "md5_hash": "",
            },
            {
                "id": 2,
                "file_name": "scph5502.bin",
                "file_path": "bios/ps/scph5502.bin",
                "file_size_bytes": 512,
                "md5_hash": "",
            },
        ]
        with patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list):
            # Precondition: files really are dicts, not BiosFileEntry objects —
            # subscripting a string key would raise on a BiosFileEntry instance.
            status: dict[str, Any] = await fw.check_platform_bios("psx")
            assert status["files"][0]["file_name"] == "scph5501.bin"
            assert status["files"][0]["downloaded"] is True

            result = await fw.delete_platform_bios("psx")

        # (b) success/deleted_count response is correct: only the one downloaded.
        assert result["success"] is True
        assert result["deleted_count"] == 1
        # (a) the downloaded file is removed via the firmware file store...
        assert str(bios_dir / "scph5501.bin") not in store.files
        # ...and its BiosFile record is pruned (matched under firmware slug "ps").
        assert plugin._uow.bios_files.get("ps", "scph5501.bin") is None

    @pytest.mark.asyncio
    async def test_delete_platform_bios_no_files(self, fw):
        """Deleting BIOS when none exist returns success with 0."""

        async def mock_check(slug, active_core_so=None):
            return {"needs_bios": False}

        fw.check_platform_bios = mock_check

        result = await fw.delete_platform_bios("snes")
        assert result["success"] is True
        assert result["deleted_count"] == 0

    @pytest.mark.asyncio
    async def test_delete_platform_bios_skips_not_downloaded(self, fw, tmp_path):
        """Only files with downloaded=True are deleted (real asdict dict shape)."""

        async def mock_check(slug, active_core_so=None):
            return {
                "needs_bios": True,
                "server_count": 2,
                "local_count": 0,
                "all_downloaded": False,
                "files": [
                    asdict(
                        BiosFileEntry(
                            file_name="bios1.bin",
                            downloaded=False,
                            local_path="/fake/path1",
                            description="bios1.bin",
                            wanted="unknown",
                            required_by_active=False,
                            cores={},
                            used_by_active=True,
                        )
                    ),
                    asdict(
                        BiosFileEntry(
                            file_name="bios2.bin",
                            downloaded=False,
                            local_path="/fake/path2",
                            description="bios2.bin",
                            wanted="unknown",
                            required_by_active=False,
                            cores={},
                            used_by_active=True,
                        )
                    ),
                ],
            }

        fw.check_platform_bios = mock_check

        result = await fw.delete_platform_bios("psx")
        assert result["success"] is True
        assert result["deleted_count"] == 0


class TestCheckPlatformBiosRequired:
    @pytest.mark.asyncio
    async def test_required_counts(self, fw, tmp_path):
        """check_platform_bios includes required_count/required_downloaded."""
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

        _declare(
            fw,
            ("required1.bin", "Required BIOS 1", True),
            ("required2.bin", "Required BIOS 2", True),
            ("optional1.bin", "Optional firmware", False),
        )

        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

        result = await fw.check_platform_bios("dc")
        assert result["needs_bios"] is True
        assert result["required_count"] == 2
        assert result["required_downloaded"] == 0
        assert result["server_count"] == 3
        # No required file downloaded → bios_level 'missing' (single source of
        # truth: domain.bios_status.compute_bios_level, threaded off this payload, #461).
        assert result["bios_level"] == "missing"

    @pytest.mark.asyncio
    async def test_all_required_downloaded(self, fw, tmp_path):
        """When all required files are downloaded, counts reflect this."""
        # Create downloaded required files (flat in bios root — nothing declares a placement)
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

        _declare(
            fw,
            ("required1.bin", "Required BIOS 1", True),
            ("required2.bin", "Required BIOS 2", True),
            ("optional1.bin", "Optional firmware", False),
        )

        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
            result = await fw.check_platform_bios("dc")
        assert result["needs_bios"] is True
        assert result["required_count"] == 2
        assert result["required_downloaded"] == 2
        assert result["local_count"] == 2
        # all_downloaded is False because optional1.bin is not downloaded
        assert result["all_downloaded"] is False
        # All required files present → bios_level 'ok' (the required-file branch
        # of compute_bios_level wins over all_downloaded, #461).
        assert result["bios_level"] == "ok"

    @pytest.mark.asyncio
    async def test_some_required_downloaded_bios_level_partial(self, fw, tmp_path):
        """One of two required files downloaded → bios_level 'partial' (#461)."""
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        (bios_dir / "required1.bin").write_bytes(b"\x00" * 100)
        # Leave required2.bin not downloaded

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
        ]

        _declare(fw, ("required1.bin", "Required BIOS 1", True), ("required2.bin", "Required BIOS 2", True))

        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
            result = await fw.check_platform_bios("dc")
        assert result["needs_bios"] is True
        assert result["required_count"] == 2
        assert result["required_downloaded"] == 1
        # One required file present, the other missing → bios_level 'partial'.
        assert result["bios_level"] == "partial"

    @pytest.mark.asyncio
    async def test_per_file_required_and_description(self, fw, tmp_path):
        """Individual files carry the declaring core's required flag and description."""
        firmware_list = [
            {"id": 1, "file_name": "bios.bin", "file_path": "bios/dc/bios.bin", "file_size_bytes": 100, "md5_hash": ""},
        ]

        _declare(fw, ("bios.bin", "Dreamcast BIOS", True))

        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

        result = await fw.check_platform_bios("dc")
        assert result["files"][0]["required_by_active"] is True
        assert result["files"][0]["description"] == "Dreamcast BIOS"

    @pytest.mark.asyncio
    async def test_files_no_emulator_asks_for_are_answered_not_unknown(self, fw, tmp_path):
        """Every emulator read and two files unclaimed → they are answered for.

        The ``fw`` fixture's ES-DE offers exactly the libretro core ``_declare``
        attributes its wants to, so the scope is real and complete — which is
        what makes ``not_needed`` the honest answer here. On an empty scope the
        same listing must read ``unknown``
        (:meth:`test_a_platform_with_no_libretro_core_answers_for_nothing`), and
        this assertion would hold vacuously.
        """
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

        # Only "known.bin" is declared by an installed emulator.
        _declare(fw, ("known.bin", "Known BIOS", True))

        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

        result = await fw.check_platform_bios("dc")
        assert result["needs_bios"] is True
        assert result["unknown_count"] == 0
        assert result["known_count"] == 1
        wanted = {f["file_name"]: f["wanted"] for f in result["files"]}
        assert wanted["known.bin"] == "needed"
        assert wanted["mystery.bin"] == "not_needed"
        assert wanted["alien.bin"] == "not_needed"

    @pytest.mark.asyncio
    async def test_the_same_files_read_unknown_when_an_emulator_could_not_be_asked(self, tmp_path):
        """The identical listing, answered against a platform with an unread emulator.

        Same server files, same declarations — only the reading changes, and the
        two unclaimed files go from a finished "nothing needs these" to "nothing
        could be established". Under the collapsed boolean both said the same
        thing.
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {"id": 1, "file_name": "known.bin", "file_path": "bios/dc/known.bin", "file_size_bytes": 1, "md5_hash": ""},
            {
                "id": 2,
                "file_name": "mystery.bin",
                "file_path": "bios/dc/mystery.bin",
                "file_size_bytes": 1,
                "md5_hash": "",
            },
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            core_info=FakeCoreInfoProvider(options=[libretro_option("flycast_libretro", "Flycast")]),
            firmware_resolver=FakeFirmwareResolver(unread_cores=frozenset({"flycast_libretro"})),
        )
        _declare(fw, ("known.bin", "Known BIOS", True))
        _inline_executor(fw)

        result = await fw.check_platform_bios("dc")

        wanted = {f["file_name"]: f["wanted"] for f in result["files"]}
        assert wanted["known.bin"] == "needed"
        assert wanted["mystery.bin"] == "unknown"
        assert result["unknown_count"] == 1

    @pytest.mark.asyncio
    async def test_a_platform_with_no_libretro_core_answers_for_nothing(self, tmp_path):
        """A standalone-only system reads grey, never a green "nothing needs these".

        ES-DE is readable and offers this system one emulator — a standalone
        one, so the libretro scope is empty. 35 of ES-DE's 172 systems are in
        that position, ``ps3`` (RPCS3) among them, and it is a mapped RomM
        platform: answering the empty scope as a complete reading classified
        every server file ``not_needed``, put ``required_count`` at 0 and
        reported "All ready" over firmware RPCS3 will not boot without.
        """
        from tests.fakes.fake_core_info_provider import standalone_option

        romm_api = MagicMock()
        romm_api.list_firmware.return_value = [
            {"id": 1, "file_name": "PS3UPDAT.PUP", "file_path": "bios/ps3/PS3UPDAT.PUP", "file_size_bytes": 1},
        ]
        fw = _make_firmware_service(
            romm_api=romm_api,
            core_info=FakeCoreInfoProvider(options=[standalone_option("%EMULATOR_RPCS3% %ROM%", "RPCS3")]),
            retrodeck_paths=FakeRetroDeckPaths(bios=str(tmp_path / "bios")),
        )
        _inline_executor(fw)

        result = await fw.check_platform_bios("ps3")

        assert [f["wanted"] for f in result["files"]] == ["unknown"]
        assert result["known_count"] == 0
        assert result["unknown_count"] == 1
        assert result["bios_level"] == "unknown"


class TestCheckPlatformBiosSlugNormalization:
    """check_platform_bios resolves slug→system for the active-core INPUT, keeps raw slug for BIOS.

    The firmware ``file_path`` and bios registry are keyed on the raw RomM
    platform slug (BIOS-folder vocabulary, ADR-0010 §4). The active-core read used
    to filter the firmware list must instead receive the resolved RetroDECK system.
    The method no longer reads ``get_available_cores`` — core info is served via the
    dedicated ``get_platform_core_info`` path (#923).
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
        core_info = FakeCoreInfoProvider(
            active_core=("flycast_libretro", "Flycast"),
            available_cores=[{"label": "Flycast", "so": "flycast_libretro"}],
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
        _declare(fw, ("boot.bin", "Boot", True))

        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

        result = await fw.check_platform_bios(slug)

        # RAW slug matched the firmware file_path, so a file is found.
        assert result["needs_bios"] is True
        assert result["server_count"] == 1
        # Both core read seams received the NORMALIZED system.
        assert core_info.active_core_calls == [system]
        assert core_info.emulator_options_calls == [system]
        assert resolver.calls == [(slug, None)]


class TestCheckPlatformBiosNoCoreFields:
    """check_platform_bios returns BIOS status only — never core fields (#923).

    Core data (active_core / active_core_label / available_cores) is served through
    the dedicated ``get_platform_core_info`` path, not the BIOS payload. Both the
    ``needs_bios=False`` (empty / offline) branches and the ``needs_bios=True`` branch
    must be free of core fields.
    """

    @pytest.mark.asyncio
    async def test_server_reachable_no_firmware_omits_core_fields(self):
        """Server reachable, no firmware for the platform → no core fields."""
        core_info = FakeCoreInfoProvider(
            active_core=("genesisplusgx_libretro", "Genesis Plus GX"),
            available_cores=[
                {"label": "Genesis Plus GX", "core_so": "genesisplusgx_libretro"},
                {"label": "PicoDrive", "core_so": "picodrive_libretro"},
            ],
        )
        fw = _make_firmware_service(core_info=core_info)
        # no emulator declares anything here

        # No firmware on the server matches the platform → collect_firmware_status
        # returns nothing, hitting the empty-files needs_bios=False branch.
        _stub_listing(fw, [])
        _inline_executor(fw)

        result = await fw.check_platform_bios("sms")

        assert result == {"needs_bios": False}
        assert "active_core" not in result
        assert "active_core_label" not in result
        assert "available_cores" not in result

    @pytest.mark.asyncio
    async def test_offline_with_no_demand_omits_core_fields(self, plugin, fw):
        """Server unreachable and no emulator wants anything → no core fields."""
        core_info = FakeCoreInfoProvider(
            active_core=("genesisplusgx_libretro", "Genesis Plus GX"),
            available_cores=[
                {"label": "Genesis Plus GX", "core_so": "genesisplusgx_libretro"},
                {"label": "PicoDrive", "core_so": "picodrive_libretro"},
            ],
        )
        fw = _make_firmware_service(romm_api=plugin._romm_api, core_info=core_info)
        # no emulator declares anything here
        fw._loop = asyncio.get_event_loop()

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")):
            result = await fw.check_platform_bios("sms")

        # No emulator wants anything and the whole scope was read, so "needs
        # none" is a real answer even with the listing unavailable — and it
        # still carries no core fields.
        assert result == {"needs_bios": False}
        assert "active_core" not in result
        assert "available_cores" not in result

    @pytest.mark.asyncio
    async def test_needs_bios_true_omits_core_fields(self, tmp_path):
        """needs_bios=True branch carries BIOS counts/files only — no core fields."""
        core_info = FakeCoreInfoProvider(
            active_core=("gpsp_libretro", "gpSP"),
            available_cores=[
                {"label": "gpSP", "so": "gpsp_libretro"},
                {"label": "mGBA", "so": "mgba_libretro"},
            ],
        )
        fw = _make_firmware_service(core_info=core_info)

        firmware_list = [
            {
                "id": 1,
                "file_name": "gba_bios.bin",
                "file_path": "bios/gba/gba_bios.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]
        _declare(fw, ("gba_bios.bin", "GBA BIOS", True))
        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))):
            result = await fw.check_platform_bios("gba")

        assert result["needs_bios"] is True
        assert result["server_count"] == 1
        assert "active_core" not in result
        assert "active_core_label" not in result
        assert "available_cores" not in result


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

        fw._core_info.active_core = (_TEST_CORE, "Test Core")
        _declare(fw, ("required.bin", "Required BIOS", True), ("optional.bin", "Optional firmware", False))

        fw._loop = asyncio.get_event_loop()

        download_called_ids = []

        async def fake_download_firmware(fw_id, _placements):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw, "_download_one", side_effect=fake_download_firmware),
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
        core_info = FakeCoreInfoProvider(active_core=("flycast_libretro", "Flycast"))
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
        # The requiring core is the one the NORMALIZED system resolves to, in the
        # plugin's own bare identifier space (no ".so").
        _resolver(fw).declare("boot.bin", required_by=["flycast_libretro"], description="Boot")
        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

        download_called_ids: list[int] = []

        async def fake_download_firmware(fw_id, _placements):
            download_called_ids.append(fw_id)
            return {"success": True}

        with patch.object(fw, "_download_one", side_effect=fake_download_firmware):
            result = await fw.download_required_firmware("dc")

        # RAW slug matched the firmware file_path filter, so the file is considered.
        # The per-core required flag (keyed on the active core from the NORMALIZED
        # system) marked it required, so it was downloaded.
        assert result["downloaded"] == 1
        assert download_called_ids == [1]
        # get_active_core received the NORMALIZED system, not the raw slug.
        assert core_info.active_core_calls == ["dreamcast"]
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

        _declare(fw, ("existing.bin", "Already downloaded", True), ("missing.bin", "Not yet downloaded", True))

        fw._loop = asyncio.get_event_loop()

        download_called_ids = []

        async def fake_download_firmware(fw_id, _placements):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw, "_download_one", side_effect=fake_download_firmware),
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.download_required_firmware("dc")

        assert result["success"] is True
        assert result["downloaded"] == 1
        assert 2 in download_called_ids
        assert 1 not in download_called_ids


class TestCheckPlatformBiosOffline:
    """What ``check_platform_bios`` answers when the RomM listing cannot be fetched.

    Readiness is assembled from three local inputs — which emulator is active
    (ES-DE), what it wants (the resolver), what is on disk — so an unreachable
    server does not take the answer away. It takes away the files only the server
    knows about, and the ability to download anything.
    """

    @pytest.mark.asyncio
    async def test_offline_still_answers_from_the_machine(self, plugin, tmp_path):
        """The emulators' demand is local, so the requirement survives the outage."""
        bios_dir = tmp_path / "bios"
        bios_dir.mkdir(parents=True)
        (bios_dir / "req1.bin").write_bytes(b"\x00" * 512)

        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            firmware_resolver=_dc_resolver(),
            core_info=_dc_core_info(),
            retrodeck_paths=FakeRetroDeckPaths(bios=str(bios_dir)),
        )
        fw._loop = asyncio.get_event_loop()

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")):
            result = await fw.check_platform_bios("dc")

        assert result["needs_bios"] is True
        assert result["required_count"] == 2
        assert result["required_downloaded"] == 1
        assert result["bios_level"] == "partial"
        # Every row came from the machine, so none of them can be fetched.
        assert all(f["on_server"] is False for f in result["files"])
        # The answer is real, so nothing is flagged as unestablished (#1693).
        assert "bios_status_unknown" not in result

    @pytest.mark.asyncio
    async def test_offline_with_a_complete_reading_and_no_demand_is_a_real_negative(self, plugin, fw, tmp_path):
        """Every emulator read, none wants anything — "needs none" is an answer.

        The server could still be holding files for this platform, but none of
        them is a requirement, so there is no warning to withhold.
        """
        with (
            patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")),
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))),
        ):
            result = await fw.check_platform_bios("n64")

        assert result == {"needs_bios": False}

    @pytest.mark.asyncio
    async def test_offline_with_an_incomplete_reading_says_it_does_not_know(self, plugin, tmp_path):
        """Nothing to show AND nothing established — the one payload that answers nothing.

        Reporting a confident "needs none" here would clear a shown requirement
        on ignorance (#1693).
        """
        from tests.fakes.fake_core_info_provider import libretro_option

        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            firmware_resolver=FakeFirmwareResolver(unread_cores=frozenset({"n64_libretro"})),
            core_info=FakeCoreInfoProvider(options=[libretro_option("n64_libretro", "Mupen64")]),
        )
        fw._loop = asyncio.get_event_loop()

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")):
            result = await fw.check_platform_bios("n64")

        assert result == {"needs_bios": False, "bios_status_unknown": True}

    @pytest.mark.asyncio
    async def test_a_cached_listing_still_contributes_its_own_files(self, plugin, fw, tmp_path):
        """The listing cache is what keeps the server-only rows available offline."""
        fw._firmware_cache = [
            {
                "id": 1,
                "file_name": "scph5501.bin",
                "file_path": "bios/ps/scph5501.bin",
                "file_size_bytes": 512,
                "md5_hash": "",
            },
        ]
        fw._firmware_cache_epoch = fw._clock.time()
        _declare(fw, ("scph5501.bin", "PS1 US BIOS", True))

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")):
            result = await fw.check_platform_bios("psx")

        assert result["needs_bios"] is True
        assert result["required_count"] == 1
        assert result["files"][0]["on_server"] is True

    @pytest.mark.asyncio
    async def test_online_no_firmware_is_a_real_negative(self, plugin, fw, tmp_path):
        """A successful fetch finding no firmware answers needs_bios False, unflagged.

        The counterpart to the offline cases above: this negative IS an answer, so
        it stays unflagged and consumers may clear a shown requirement on it.
        """
        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=[]),
            patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))),
        ):
            result = await fw.check_platform_bios("n64")

        assert result["needs_bios"] is False
        assert "bios_status_unknown" not in result


class TestPerCoreFiltering:
    """Per-core BIOS filtering: what the machine wants vs. what THIS launch needs.

    The two axes travel together on every file. ``wanted`` is the machine's
    answer and does not move with the core the user picked; ``required_by_active``
    and ``used_by_active`` are the launching core's, and they are what the
    missing-BIOS badge counts. A file three other cores demand is not a missing
    prerequisite for a launch on a core that never opens it.
    """

    @staticmethod
    def _gba_resolver() -> FakeFirmwareResolver:
        """The real GBA shape: gpSP will not run without the BIOS, mGBA will."""
        resolver = FakeFirmwareResolver()
        resolver.declare(
            "gba_bios.bin",
            required_by=["gpsp_libretro"],
            optional_for=["mgba_libretro"],
            description="GBA BIOS",
        )
        resolver.declare(
            "gb_bios.bin",
            optional_for=["gambatte_libretro", "mgba_libretro"],
            description="GB BIOS",
        )
        resolver.declare("sgb_bios.bin", optional_for=["mgba_libretro"], description="SGB BIOS")
        return resolver

    @staticmethod
    def _gba_firmware() -> list[dict[str, Any]]:
        return [
            {
                "id": i + 1,
                "file_name": name,
                "file_path": f"bios/gba/{name}",
                "file_size_bytes": 100 * (i + 1),
                "md5_hash": "",
            }
            for i, name in enumerate(("gba_bios.bin", "gb_bios.bin", "sgb_bios.bin"))
        ]

    def _service(self, active_core: tuple[str | None, str | None]) -> FirmwareService:
        romm_api = MagicMock()
        romm_api.list_firmware.return_value = self._gba_firmware()
        fw = _make_firmware_service(
            romm_api=romm_api,
            firmware_resolver=self._gba_resolver(),
            core_info=FakeCoreInfoProvider(active_core=active_core),
        )
        _inline_executor(fw)
        return fw

    @pytest.mark.asyncio
    async def test_the_launching_core_decides_what_counts_as_required(self, tmp_path):
        """gpSP requires the GBA BIOS, and only the files gpSP opens are counted."""
        fw = self._service(("gpsp_libretro", "gpSP"))
        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))):
            result = await fw.check_platform_bios("gba")

        assert result["needs_bios"] is True
        assert result["server_count"] == 3
        # Core fields are never served from the BIOS payload (#923).
        assert "active_core" not in result
        assert "active_core_label" not in result

        gba_file = next(f for f in result["files"] if f["file_name"] == "gba_bios.bin")
        assert gba_file["required_by_active"] is True
        assert gba_file["used_by_active"] is True
        assert gba_file["wanted"] == "needed"

        gb_file = next(f for f in result["files"] if f["file_name"] == "gb_bios.bin")
        assert gb_file["used_by_active"] is False
        assert gb_file["required_by_active"] is False
        assert gb_file["cores"] == {
            "gambatte_libretro": {"required": False},
            "mgba_libretro": {"required": False},
        }

        assert result["required_count"] == 1
        assert result["required_downloaded"] == 0

    @pytest.mark.asyncio
    async def test_a_core_that_needs_none_of_them_counts_none(self):
        """mGBA opens all three and demands none — nothing is a prerequisite."""
        fw = self._service(("mgba_libretro", "mGBA"))
        result = await fw.check_platform_bios("gba")

        assert result["server_count"] == 3
        assert result["required_count"] == 0
        for f in result["files"]:
            assert f["required_by_active"] is False
            assert f["used_by_active"] is True

    @pytest.mark.asyncio
    async def test_wanted_does_not_move_with_the_active_core(self):
        """The machine's answer about a file is the same whichever core is picked.

        This is what stops one file reading "known" on one surface and "unknown"
        on another: only the launch-scoped fields differ between the two runs.
        """
        gpsp = await self._service(("gpsp_libretro", "gpSP")).check_platform_bios("gba")
        mgba = await self._service(("mgba_libretro", "mGBA")).check_platform_bios("gba")

        assert {f["file_name"]: f["wanted"] for f in gpsp["files"]} == {
            f["file_name"]: f["wanted"] for f in mgba["files"]
        }
        assert gpsp["required_count"] != mgba["required_count"]

    @pytest.mark.asyncio
    async def test_an_unresolved_core_falls_back_to_every_declaring_emulator(self):
        """No resolvable core → a file any emulator requires counts as required.

        The documented safe default: with nothing to filter by, the platform
        shows what it would need in the worst case rather than clearing the
        warning.
        """
        fw = self._service((None, None))
        result = await fw.check_platform_bios("gba")

        assert result["server_count"] == 3
        assert "active_core" not in result
        gba_file = next(f for f in result["files"] if f["file_name"] == "gba_bios.bin")
        assert gba_file["required_by_active"] is True
        assert gba_file["used_by_active"] is True
        assert result["required_count"] == 1

    @pytest.mark.asyncio
    async def test_a_core_no_declaration_names_requires_nothing(self):
        """An active core no placement mentions opens none of these files."""
        fw = self._service(("snes9x_libretro", "Snes9x"))
        result = await fw.check_platform_bios("gba")

        assert result["required_count"] == 0
        for f in result["files"]:
            assert f["used_by_active"] is False
            assert f["required_by_active"] is False


class TestCheckPlatformBiosPreResolvedCore:
    """R8: ``check_platform_bios`` takes a pre-resolved ``active_core_so``.

    The per-game game-detail path resolves the active ``.so`` upstream (folding
    the ``emulator_override`` pin) and passes it in; the platform-level callers
    pass ``None`` to mean "use the system default". The filter's ``required_count``
    must follow the pre-resolved core, not the system default.
    """

    def _gba_two_core_service(self, fw, firmware_list):
        """Wire a gba demand where gpSP requires gba_bios.bin and mGBA does not."""
        _resolver(fw).declare(
            "gba_bios.bin",
            required_by=["gpsp_libretro"],
            optional_for=["mgba_libretro"],
            description="GBA BIOS",
        )
        _stub_listing(fw, firmware_list)
        _inline_executor(fw)

    @pytest.mark.asyncio
    async def test_pre_resolved_core_drives_filter_over_system_default(self, fw, tmp_path):
        """A passed-in ``active_core_so`` overrides the system default for required_count.

        The system default (set on the fake) is mGBA — which treats gba_bios.bin as
        optional. Passing the per-game override ``gpsp_libretro`` flips the file to
        required, proving the pre-resolved core (not the system default) feeds the
        filter and that ``get_active_core`` is NOT consulted when a core is supplied.
        """
        firmware_list = [
            {"id": 1, "file_name": "gba_bios.bin", "file_path": "bios/gba/gba_bios.bin", "md5_hash": ""},
        ]
        self._gba_two_core_service(fw, firmware_list)
        # System default = mGBA (optional). The per-game override should win.
        fw._core_info.active_core = ("mgba_libretro", "mGBA")

        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))):
            result = await fw.check_platform_bios("gba", active_core_so="gpsp_libretro")

        assert result["needs_bios"] is True
        assert result["required_count"] == 1  # gpSP requires gba_bios.bin
        # The pre-resolved core short-circuits the system-default read entirely.
        assert fw._core_info.active_core_calls == []

    @pytest.mark.asyncio
    async def test_none_falls_back_to_system_default(self, fw, tmp_path):
        """``active_core_so=None`` resolves the system default via ``get_active_core``.

        Same declarations, no per-game core: the platform-level path reads the system
        default (mGBA → optional) so ``required_count`` is 0 — the opposite of the
        override case, locking in the result-flip.
        """
        firmware_list = [
            {"id": 1, "file_name": "gba_bios.bin", "file_path": "bios/gba/gba_bios.bin", "md5_hash": ""},
        ]
        self._gba_two_core_service(fw, firmware_list)
        fw._core_info.active_core = ("mgba_libretro", "mGBA")

        with patch.object(fw, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))):
            result = await fw.check_platform_bios("gba")

        assert result["needs_bios"] is True
        assert result["required_count"] == 0  # mGBA treats gba_bios.bin as optional
        # None → the system default was read once for the system "gba".
        assert fw._core_info.active_core_calls == ["gba"]


class TestDownloadFirmwareErrors:
    """Tests for download_firmware error handling."""

    @pytest.mark.asyncio
    async def test_fetch_metadata_error(self, fw):
        """Fetch firmware metadata failure returns error."""
        assert isinstance(fw._romm_api, MagicMock)
        fw._romm_api.get_firmware.side_effect = Exception("not found")
        _inline_executor(fw)

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
        assert "reason" in result
        assert "Invalid firmware metadata" in result["message"]
        # The renamed/downloaded file was cleaned up — nothing left dangling.
        assert not os.path.exists(os.path.join(str(bios_dir), "orphan.bin"))
        # No BiosFile record persisted (empty slug key would be ("", "orphan.bin")).
        assert plugin._uow.bios_files.get("", "orphan.bin") is None
        assert list(plugin._uow.bios_files.iter_all()) == []


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
        _stub_listing(fw, firmware_list)
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
        _stub_listing(fw, firmware_list)
        fw._firmware_cache = None

        with patch.object(plugin._uow.firmware_cache, "replace_all", side_effect=OSError("disk full")):
            result = fw._get_firmware_list()

        assert result == firmware_list
        assert fw._firmware_cache == firmware_list


class TestDeletePlatformBiosIOLogsWarnings:
    """Coverage for the OSError-warning path in _delete_platform_bios_io."""

    @pytest.mark.asyncio
    async def test_logs_warning_and_collects_error_when_remove_fails(self, plugin, fw, caplog):
        """A per-file OSError surfaces as a logger.warning and an error entry."""
        import logging

        fake_files = FakeFirmwareFileStore()
        fake_files.remove_failures.add("/fake/bios/scph5501.bin")
        fw._firmware_file_store = fake_files

        async def mock_check(slug, active_core_so=None):
            return {
                "needs_bios": True,
                "files": [
                    asdict(
                        BiosFileEntry(
                            file_name="scph5501.bin",
                            downloaded=True,
                            local_path="/fake/bios/scph5501.bin",
                            description="PS1 BIOS",
                            wanted="needed",
                            required_by_active=True,
                            cores={},
                            used_by_active=True,
                        )
                    ),
                    asdict(
                        BiosFileEntry(
                            file_name="scph5502.bin",
                            downloaded=True,
                            local_path="/fake/bios/scph5502.bin",
                            description="PS1 BIOS (EU)",
                            wanted="needed",
                            required_by_active=True,
                            cores={},
                            used_by_active=True,
                        )
                    ),
                ],
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
