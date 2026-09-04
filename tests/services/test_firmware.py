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
from fakes.fake_firmware_resolver import FakeFirmwareResolver, FakeFolderVerdicts
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
from domain.firmware_wants import FolderVerdict
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
    firmware_folder_verdicts: FakeFolderVerdicts | None = None,
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

    The resolver is pointed at the same BIOS root and file store the service
    gets, because on a real machine they are one directory read by two
    readers — so a test that puts a file where the platform expects it gets
    the same ``present`` from both. A test that hands in a resolver with a root
    of its own keeps it, and one that wants the two to disagree says so per
    file (``FakeFirmwareResolver.declare(present=...)``).
    """
    import decky

    store = firmware_file_store if firmware_file_store is not None else FirmwareFileAdapter()
    paths = retrodeck_paths if retrodeck_paths is not None else FakeRetroDeckPaths()
    resolver = firmware_resolver if firmware_resolver is not None else FakeFirmwareResolver()
    if resolver.bios_root is None:
        resolver.bios_root = paths.bios_path()
        resolver.present_probe = store.exists

    return FirmwareService(
        config=FirmwareServiceConfig(
            romm_api=romm_api if romm_api is not None else MagicMock(),
            loop=asyncio.get_event_loop(),
            logger=logger if logger is not None else decky.logger,
            clock=clock if clock is not None else _make_clock(),
            firmware_file_store=store,
            firmware_resolver=resolver,
            firmware_folder_verdicts=firmware_folder_verdicts
            if firmware_folder_verdicts is not None
            else FakeFolderVerdicts(),
            retrodeck_paths=paths,
            core_info=core_info if core_info is not None else FakeCoreInfoProvider(),
            resolve_system=resolve_system if resolve_system is not None else FakeSystemResolver(),
            platform_core_reader=platform_core_reader if platform_core_reader is not None else FakePlatformCoreReader(),
            uow_factory=uow_factory if uow_factory is not None else FakeUnitOfWorkFactory(),
        ),
    )


def _set_loop(fw: FirmwareService, loop) -> None:
    """Point every sub-service that offloads work at *loop*.

    The loop is config-carried, so each sub-service holds its own reference and
    the façade holds none. A test that swaps the loop has to reach all three, or
    the swap lands on nothing and the offloaded hop runs on the loop the fixture
    happened to construct the service under.
    """
    fw._status._loop = loop
    fw._downloads._loop = loop
    fw._deletion._loop = loop


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
    _set_loop(fw, loop)


def _resolver(fw: FirmwareService) -> FakeFirmwareResolver:
    """The fake resolver behind *fw*, for tests that seed it after construction."""
    resolver = fw._demand._firmware_resolver
    assert isinstance(resolver, FakeFirmwareResolver)
    return resolver


def _resolver_reads(fw: FirmwareService, bios_dir) -> None:
    """Point the resolver at *bios_dir* as well — one directory, two readers.

    For a test that swaps the service's BIOS root after construction, where
    ``_make_firmware_service`` could not wire the pair up itself.
    """
    _resolver(fw).bios_root = str(bios_dir)


def _stub_listing(fw: FirmwareService, firmware_list: list[dict[str, Any]]) -> None:
    """Answer ``list_firmware`` with *firmware_list* on the service's API stub."""
    api = fw._config.romm_api
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
    """Ensure plugin.loop and the firmware sub-services match the running event loop."""
    loop = asyncio.get_event_loop()
    plugin.loop = loop
    _set_loop(fw, loop)


# Shorthand to access the firmware service from plugin
@pytest.fixture
def fw(plugin):
    return plugin._firmware_service


# What the façade holds. Everything else the service needs lives on a sub-service.
_FACADE_ATTRIBUTES = frozenset({"_config", "_listing", "_demand", "_status", "_downloads", "_deletion"})


@pytest.fixture(autouse=True)
def _no_dead_rebind_on_the_facade(fw):
    """Fail an assignment to a name the façade does not have.

    Python answers such an assignment by creating the name, so ``fw._loop = loop``
    — a line meaning to swap the loop the offloaded hops run on — lands on the
    façade, does nothing, and leaves the test green against whichever loop the
    fixture built the service under. Nothing else notices: the call it was meant
    to redirect still works, just against the old object. This makes the
    assignment itself the failure.

    Two assignments stay allowed, and only one of them is safe. Shadowing a name
    the façade already answers to is deliberate — ``fw.check_platform_bios = mock``
    replaces a real method rather than inventing one. Rebinding one of the six
    construction slots is NOT: ``fw._listing = FakeListing()`` reaches
    ``invalidate_firmware_cache`` and nothing else, because every sub-service bound
    its own reference to the listing when it was built, and the same holds for
    ``_demand``. A swap has to reach every holder, so build the service around the
    fake rather than rebinding it afterwards.
    """
    yield
    stray = sorted(name for name in vars(fw) if name not in _FACADE_ATTRIBUTES and not hasattr(type(fw), name))
    assert not stray, (
        f"assigned {stray} to the façade, which holds no such name — the assignment did nothing. "
        f"Firmware state lives on a sub-service: assign inside fw._listing / fw._demand / fw._status / "
        f"fw._downloads / fw._deletion, or _set_loop(fw, loop) for the event loop."
    )


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


class TestTheFacadeOnlyDelegates:
    """The façade wires the sub-services and delegates. It implements nothing itself.

    Stated as the whole surface rather than as a list of banned names, so it fails
    in both directions: a helper the split relocated cannot come back here, and a
    new method cannot arrive without someone deciding it belongs on the façade
    rather than in the module that owns its job. A second implementation here is
    what the split cost — presence, the destination it is read at, and the RomM
    listing each have one owner now, and a convenience wrapper on the façade would
    answer from whatever it happened to hold.
    """

    def test_the_facade_defines_exactly_its_delegations(self):
        own = {name for name, value in vars(FirmwareService).items() if callable(value) and not name.startswith("__")}
        assert own == {
            "invalidate_firmware_cache",
            "get_firmware_status",
            "check_platform_bios",
            "download_firmware",
            "download_all_firmware",
            "download_platform_firmware_file",
            "download_required_firmware",
            "delete_platform_bios",
            "delete_bios_file",
            "delete_bios_folder",
        }

    def test_the_facade_holds_only_its_sub_services(self, fw):
        """Construction leaves the config and the five sub-services, and nothing else."""
        assert set(vars(fw)) == _FACADE_ATTRIBUTES


class TestFirmwareDestPath:
    """Where a firmware file lands: the resolver's placement, or flat as a fallback."""

    def test_flat_default_when_nothing_declares_the_file(self, fw, tmp_path):
        """A server file no emulator asks for has no stated layout — flat in the root."""
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        fw._demand._retrodeck_paths = FakeRetroDeckPaths(bios=bios)
        firmware = {"file_name": "bios.bin", "file_path": "bios/n64/bios.bin"}
        dest = fw._demand.dest_path(firmware, None)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "bios.bin")

    def test_subdirectory_placement_is_honoured(self, fw, tmp_path):
        """A placement below the firmware root places the file in that subdirectory."""
        placement = _resolver(fw).declare(
            "dc_boot.bin", required_by=["flycast_libretro"], relative_path="dc/dc_boot.bin"
        )
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        fw._demand._retrodeck_paths = FakeRetroDeckPaths(bios=bios)
        firmware = {"file_name": "dc_boot.bin", "file_path": "bios/dc/dc_boot.bin"}
        dest = fw._demand.dest_path(firmware, placement)
        assert dest == os.path.join(str(tmp_path), "retrodeck", "bios", "dc", "dc_boot.bin")

    def test_placement_without_a_subdirectory_goes_flat(self, fw, tmp_path):
        placement = _resolver(fw).declare("scph5501.bin", required_by=["mednafen_psx_libretro"])
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=bios)):
            firmware = {"file_name": "scph5501.bin", "file_path": "bios/ps/scph5501.bin"}
            dest = fw._demand.dest_path(firmware, placement)
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
        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=bios)):
            firmware = {"file_name": "bios7.bin", "file_path": "bios/nds/bios7.bin"}
            dest = fw._demand.dest_path(firmware, placement)
            assert dest == os.path.join(bios, "bios7.bin")

    def test_uses_dynamic_bios_path(self, fw, tmp_path):
        """Uses ``retrodeck_paths.bios_path()`` for the base directory."""
        sd_bios = "/run/media/deck/Emulation/retrodeck/bios"
        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=sd_bios)):
            firmware = {"file_name": "fw.bin", "file_path": "bios/saturn/fw.bin"}
            dest = fw._demand.dest_path(firmware, None)
            assert dest == os.path.join(sd_bios, "fw.bin")

    def test_a_traversing_placement_is_refused(self, fw, tmp_path):
        """``safe_join`` guards the placement too, not only the server file name."""
        from lib.path_safety import PathTraversalError

        placement = _resolver(fw).declare("evil.bin", required_by=["x_libretro"], relative_path="../evil.bin")
        bios = os.path.join(str(tmp_path), "retrodeck", "bios")
        with (
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=bios)),
            pytest.raises(PathTraversalError),
        ):
            fw._demand.dest_path({"file_name": "evil.bin"}, placement)

    def test_a_declaration_the_distribution_links_onto_the_root_is_still_a_destination(self, fw, tmp_path):
        """RetroDECK points ``<bios>/pcsx2/bios`` at ``<bios>``, so LRPS2's folder IS the root.

        ``safe_join`` refuses the base by default, which would drop a real
        requirement instead of guarding anything — a declared location opts in.
        """
        bios_dir = tmp_path / "retrodeck" / "bios"
        (bios_dir / "pcsx2").mkdir(parents=True)
        (bios_dir / "pcsx2" / "bios").symlink_to(bios_dir)
        placement = _resolver(fw).declare("bios", required_by=["pcsx2_libretro"], relative_path="pcsx2/bios")
        fw._demand._retrodeck_paths = FakeRetroDeckPaths(bios=str(bios_dir))

        assert fw._demand.dest_path({"file_name": "bios"}, placement) == os.path.realpath(str(bios_dir))

    def test_a_server_file_name_may_not_land_on_the_root(self, fw, tmp_path):
        """The opt-in is the declaration's, not the listing's — an empty name is no file."""
        from lib.path_safety import PathTraversalError

        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        fw._demand._retrodeck_paths = FakeRetroDeckPaths(bios=str(bios_dir))

        with pytest.raises(PathTraversalError):
            fw._demand.dest_path({"file_name": ""}, None)


class TestPresenceComesFromTheReading:
    """Who answers "is the file there": the reading for a declared row, us for the rest."""

    _CORE = "flycast_libretro"

    def _service(self, plugin, tmp_path, resolver, store: FakeFirmwareFileStore | None = None):
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_file_store=store if store is not None else FakeFirmwareFileStore(),
            firmware_resolver=resolver,
            core_info=FakeCoreInfoProvider(
                active_core=(self._CORE, "Flycast"), options=[libretro_option(self._CORE, "Flycast")]
            ),
            retrodeck_paths=FakeRetroDeckPaths(bios=str(tmp_path / "bios")),
        )
        _inline_executor(fw)
        _stub_listing(
            fw,
            [{"id": 1, "file_name": "dc_boot.bin", "file_path": "bios/dc/dc_boot.bin", "file_size_bytes": 8}],
        )
        return fw

    @staticmethod
    def _store_holding(tmp_path, name: str) -> FakeFirmwareFileStore:
        """A file store with *name* sitting in the BIOS root, whatever the reading says."""
        return FakeFirmwareFileStore({os.path.join(str(tmp_path / "bios"), name): b"\x00"})

    @pytest.mark.asyncio
    async def test_a_declared_row_is_there_when_the_reading_says_so(self, plugin, tmp_path):
        """Nothing in our own store, and the row still reads present.

        The reading followed the symlinks to the destination the emulator will
        open; a probe of the path assembled here answers about a different
        place, and that divergence is what put a satisfied LRPS2 requirement on
        the page as a red missing row.
        """
        resolver = FakeFirmwareResolver()
        resolver.declare("dc_boot.bin", required_by=[self._CORE], present=True)
        fw = self._service(plugin, tmp_path, resolver)

        result = await fw.check_platform_bios("dc")

        assert result["files"][0]["downloaded"] is True
        assert result["required_downloaded"] == 1

    @pytest.mark.asyncio
    async def test_a_declared_row_is_absent_when_the_reading_says_so(self, plugin, tmp_path):
        """And the other direction: our store holding it does not override the reading."""
        resolver = FakeFirmwareResolver()
        resolver.declare("dc_boot.bin", required_by=[self._CORE], present=False)
        fw = self._service(plugin, tmp_path, resolver, self._store_holding(tmp_path, "dc_boot.bin"))

        result = await fw.check_platform_bios("dc")

        assert result["files"][0]["downloaded"] is False

    @pytest.mark.asyncio
    async def test_a_destination_the_reading_could_not_look_at_is_not_a_claim(self, plugin, tmp_path):
        """ "Could not look" is not "it is there" — the row stays missing."""
        resolver = FakeFirmwareResolver()
        resolver.declare("dc_boot.bin", required_by=[self._CORE])
        resolver.bios_root = ""  # no place for the fake to take a reading, so it withholds one
        # The store holds it, so a fallback to our own probe would read green here.
        fw = self._service(plugin, tmp_path, resolver, self._store_holding(tmp_path, "dc_boot.bin"))

        result = await fw.check_platform_bios("dc")

        assert result["files"][0]["downloaded"] is False

    @pytest.mark.asyncio
    async def test_a_row_nothing_declares_is_ours_to_probe(self, plugin, tmp_path):
        """One of the rows our own check covers: a library file no emulator asked for."""
        fw = self._service(plugin, tmp_path, FakeFirmwareResolver(), self._store_holding(tmp_path, "dc_boot.bin"))

        result = await fw.check_platform_bios("dc")

        assert result["files"][0]["downloaded"] is True
        assert result["files"][0]["wanted"] == "not_needed"

    @pytest.mark.asyncio
    async def test_a_placement_we_cannot_honour_is_ours_to_probe_too(self, plugin, tmp_path):
        """The other one: a declared destination outside the BIOS root.

        The file then goes by this service's own flat default, which is not the
        destination the reading was taken at — an emulator's own tree holding
        it says nothing about the BIOS root.
        """
        resolver = FakeFirmwareResolver()
        # The reading says absent; it was taken somewhere this service will not write.
        resolver.declare("dc_boot.bin", required_by=[self._CORE], relative_path=None, present=False)
        fw = self._service(plugin, tmp_path, resolver, self._store_holding(tmp_path, "dc_boot.bin"))

        result = await fw.check_platform_bios("dc")

        assert result["files"][0]["downloaded"] is True


class TestDestinationReadingsReachBothSurfaces:
    """``supplied_by`` and ``declared_kind`` travel to the game page and the platform detail."""

    _CORE = "flycast_libretro"

    def _service(self, plugin, tmp_path, resolver):
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_resolver=resolver,
            core_info=FakeCoreInfoProvider(
                active_core=(self._CORE, "Flycast"), options=[libretro_option(self._CORE, "Flycast")]
            ),
            retrodeck_paths=FakeRetroDeckPaths(bios=str(tmp_path / "bios")),
        )
        _inline_executor(fw)
        return fw

    @pytest.mark.asyncio
    async def test_the_game_page_row_names_the_supplying_distribution(self, plugin, tmp_path):
        resolver = FakeFirmwareResolver()
        resolver.declare("codehandler.bin", required_by=[self._CORE], present=True, supplied_by="RetroDECK")
        fw = self._service(plugin, tmp_path, resolver)
        _stub_listing(fw, [])

        result = await fw.check_platform_bios("dc")

        assert result["files"][0]["supplied_by"] == "RetroDECK"
        assert result["files"][0]["declared_kind"] == "file"

    @pytest.mark.asyncio
    async def test_the_system_page_row_names_the_folder_declaration(self, plugin, tmp_path):
        _seed_rom(plugin._uow, rom_id=51, platform_slug="dc", app_id=1)
        resolver = FakeFirmwareResolver()
        resolver.declare(
            "bios", required_by=[self._CORE], relative_path="pcsx2/bios", present=True, declares_directory=True
        )
        fw = self._service(plugin, tmp_path, resolver)
        _stub_listing(fw, [])

        result = await fw.get_firmware_status()
        row = next(p for p in result["platforms"] if p["platform_slug"] == "dc")["files"][0]

        assert row["declared_kind"] == "directory"
        assert row["supplied_by"] is None
        assert row["downloaded"] is True


class TestAFolderRequirementIsAnsweredByItsContents:
    """LRPS2's ``pcsx2/bios`` is answered by what is inside it, never by its being there.

    RetroDECK links that folder onto the BIOS root, so it is present on every
    install: reading presence as the verdict reports "All required ready" over a
    PS2 system with no BIOS file at all, and reading absence reports red over a
    folder that is plainly there. The verified read settles it, and the readiness
    verdict declines only where that read established nothing.
    """

    _CORE = "pcsx2_libretro"
    _IMAGES = ("Europe  v02.00(14/06/2004)  Console 20040614-100914",)

    def _service(self, plugin, tmp_path, resolver, verdicts=None):
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_resolver=resolver,
            firmware_folder_verdicts=verdicts,
            core_info=FakeCoreInfoProvider(
                active_core=(self._CORE, "LRPS2"), options=[libretro_option(self._CORE, "LRPS2")]
            ),
            retrodeck_paths=FakeRetroDeckPaths(bios=str(tmp_path / "bios")),
        )
        _inline_executor(fw)
        _stub_listing(fw, [])
        return fw

    def _resolver(self) -> FakeFirmwareResolver:
        """What LRPS2 asks for: a folder that is there, and a file beside it."""
        resolver = FakeFirmwareResolver()
        resolver.declare(
            "bios", required_by=[self._CORE], relative_path="pcsx2/bios", present=True, declares_directory=True
        )
        resolver.declare(
            "GameIndex.yaml",
            required_by=[self._CORE],
            relative_path="pcsx2/resources/GameIndex.yaml",
            present=True,
        )
        return resolver

    def _verdicts(self, satisfied: bool | None, *, caveats=(), images=()) -> FakeFolderVerdicts:
        return FakeFolderVerdicts(
            {self._CORE: {"bios": FolderVerdict(satisfied=satisfied, images=images, caveats=caveats)}}
        )

    @pytest.mark.asyncio
    async def test_an_image_in_the_folder_completes_the_required_ratio(self, plugin, tmp_path):
        fw = self._service(plugin, tmp_path, self._resolver(), self._verdicts(True, images=self._IMAGES))

        result = await fw.check_platform_bios("ps2")

        assert result["required_count"] == 2
        assert result["required_downloaded"] == 2
        assert result["required_withheld"] == 0
        assert result["bios_level"] == "ok"

    @pytest.mark.asyncio
    async def test_the_row_names_the_images_the_read_identified(self, plugin, tmp_path):
        fw = self._service(plugin, tmp_path, self._resolver(), self._verdicts(True, images=self._IMAGES))

        result = await fw.check_platform_bios("ps2")
        row = next(row for row in result["files"] if row["file_name"] == "bios")

        assert row["satisfied"] is True
        assert row["images"] == self._IMAGES
        assert row["declared_kind"] == "directory"

    @pytest.mark.asyncio
    async def test_a_folder_holding_no_image_is_a_requirement_shown_to_be_unmet(self, plugin, tmp_path):
        """Not a declined verdict: the read happened and the answer is no."""
        verdicts = self._verdicts(False, caveats=("firmware-directory-holds-no-image",))
        fw = self._service(plugin, tmp_path, self._resolver(), verdicts)

        result = await fw.check_platform_bios("ps2")
        row = next(row for row in result["files"] if row["file_name"] == "bios")

        assert result["required_downloaded"] == 1
        assert result["required_withheld"] == 0
        assert result["bios_level"] == "partial"
        assert row["satisfied"] is False
        assert row["caveats"] == ("firmware-directory-holds-no-image",)

    @pytest.mark.asyncio
    async def test_a_read_that_established_nothing_still_declines_the_verdict(self, plugin, tmp_path):
        """The resolver could not answer, so neither ready nor missing is a claim."""
        fw = self._service(plugin, tmp_path, self._resolver(), FakeFolderVerdicts())

        result = await fw.check_platform_bios("ps2")

        assert result["required_downloaded"] == 1
        assert result["required_withheld"] == 1
        assert result["bios_level"] == "unknown"
        assert result["bios_label"] == "Unknown"

    @pytest.mark.asyncio
    async def test_the_rows_beside_the_folder_keep_their_own_answers(self, plugin, tmp_path):
        fw = self._service(plugin, tmp_path, self._resolver(), self._verdicts(True, images=self._IMAGES))

        result = await fw.check_platform_bios("ps2")
        rows = {row["file_name"]: row for row in result["files"]}

        assert rows["GameIndex.yaml"]["downloaded"] is True
        assert rows["GameIndex.yaml"]["satisfied"] is True
        assert rows["GameIndex.yaml"]["declared_kind"] == "file"
        assert rows["GameIndex.yaml"]["wanted"] == "needed"

    @pytest.mark.asyncio
    async def test_a_folder_the_machine_wide_reading_settled_is_not_asked_again(self, plugin, tmp_path):
        """An absent folder is settled by a stat, so nothing pays for a content read."""
        resolver = FakeFirmwareResolver()
        resolver.declare(
            "bios",
            required_by=[self._CORE],
            relative_path="pcsx2/bios",
            present=False,
            declares_directory=True,
            folder=FolderVerdict(satisfied=False),
        )
        verdicts = FakeFolderVerdicts()
        fw = self._service(plugin, tmp_path, resolver, verdicts)

        result = await fw.check_platform_bios("ps2")

        assert verdicts.calls == []
        assert result["required_downloaded"] == 0
        assert result["bios_level"] == "missing"

    @pytest.mark.asyncio
    async def test_a_folder_the_stat_settled_reaches_the_row_with_the_code_that_words_it(self, plugin, tmp_path):
        """Red is not enough on its own — the row has to say why, and nothing asks again.

        A folder holding no file of a size the core would open is answered
        without verification, so this row is never re-asked and the code the
        inventory carried is the only word it will ever have.
        """
        resolver = FakeFirmwareResolver()
        resolver.declare(
            "bios",
            required_by=[self._CORE],
            relative_path="pcsx2/bios",
            present=True,
            declares_directory=True,
            folder=FolderVerdict(satisfied=False),
            caveats=("firmware-directory-holds-no-candidate",),
        )
        verdicts = FakeFolderVerdicts()
        fw = self._service(plugin, tmp_path, resolver, verdicts)

        result = await fw.check_platform_bios("ps2")
        row = next(row for row in result["files"] if row["file_name"] == "bios")

        assert verdicts.calls == []
        assert row["satisfied"] is False
        assert row["caveats"] == ("firmware-directory-holds-no-candidate",)
        assert result["bios_level"] == "missing"

    @pytest.mark.asyncio
    async def test_a_platform_whose_cores_declare_no_folder_never_asks(self, plugin, tmp_path):
        """The cost is paid where the folder row is, and nowhere else."""
        resolver = FakeFirmwareResolver()
        resolver.declare("GameIndex.yaml", required_by=[self._CORE], present=True)
        verdicts = FakeFolderVerdicts()
        fw = self._service(plugin, tmp_path, resolver, verdicts)

        await fw.check_platform_bios("ps2")

        assert verdicts.calls == []

    @pytest.mark.asyncio
    async def test_the_system_page_asks_each_core_once_and_reads_the_same_verdict(self, plugin, tmp_path):
        """Every platform offering the core shares one answer — the read is per core."""
        _seed_rom(plugin._uow, rom_id=52, platform_slug="ps2", app_id=2)
        _seed_rom(plugin._uow, rom_id=53, platform_slug="ps2-alt", app_id=3)
        verdicts = self._verdicts(True, images=self._IMAGES)
        fw = self._service(plugin, tmp_path, self._resolver(), verdicts)

        result = await fw.get_firmware_status()
        platform = next(p for p in result["platforms"] if p["platform_slug"] == "ps2")

        assert verdicts.calls == [self._CORE]
        assert platform["bios_level"] == "ok"
        assert platform["required_count"] == 2
        assert platform["required_downloaded"] == 2
        assert platform["required_withheld"] == 0

    @pytest.mark.asyncio
    async def test_a_folder_no_installed_core_requires_leaves_the_verdict_alone(self, plugin, tmp_path):
        """The scope is the launching core's requirement, not every folder on the page."""
        resolver = FakeFirmwareResolver()
        resolver.declare(
            "bios", optional_for=[self._CORE], relative_path="pcsx2/bios", present=True, declares_directory=True
        )
        fw = self._service(plugin, tmp_path, resolver, FakeFolderVerdicts())

        result = await fw.check_platform_bios("ps2")

        assert result["required_withheld"] == 0
        assert result["bios_level"] == "ok"


class TestAFileWithSomethingElseAtItsDestination:
    """A directory in a declared file's way settles nothing about the requirement.

    The resolver states one shape at the destination and it is not the file, so
    neither "there" nor "absent" is a claim the reading supports — the mirror
    image of the folder whose contents could not be read.
    """

    _CORE = "flycast_libretro"

    @pytest.mark.asyncio
    async def test_an_obstructed_file_declines_the_verdict_rather_than_reading_as_present(self, plugin, tmp_path):
        resolver = FakeFirmwareResolver()
        resolver.declare(
            "dc_boot.bin",
            required_by=[self._CORE],
            present=True,
            caveats=("firmware-path-obstructed",),
        )
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_resolver=resolver,
            core_info=FakeCoreInfoProvider(
                active_core=(self._CORE, "Flycast"), options=[libretro_option(self._CORE, "Flycast")]
            ),
            retrodeck_paths=FakeRetroDeckPaths(bios=str(tmp_path / "bios")),
        )
        _inline_executor(fw)
        _stub_listing(fw, [])

        result = await fw.check_platform_bios("dc")
        row = result["files"][0]

        assert row["downloaded"] is True
        assert row["satisfied"] is None
        assert row["caveats"] == ("firmware-path-obstructed",)
        assert result["required_downloaded"] == 0
        assert result["required_withheld"] == 1
        assert result["bios_level"] == "unknown"


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
        ``active_core`` label ("PPSSPP") — the exact case the platform detail's
        label must reflect. Returns the resolved ``active_core_label``.
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
        """A per-platform pin surfaces on the platform detail's label immediately (#1305)."""
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
        _set_loop(fw, asyncio.get_event_loop())
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

        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
            result = await fw.get_firmware_status()
        assert result["success"] is True
        assert result["platforms"][0]["files"][0]["downloaded"] is True

    @pytest.mark.asyncio
    async def test_handles_api_error_with_offline_fallback(self, plugin, fw):
        # Real loop: only the HTTP list_firmware fails; the installed-slugs read
        # against the fake UoW still succeeds.
        _set_loop(fw, asyncio.get_event_loop())

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

    The platform detail reads the unknown/ok/partial/missing decision and display
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
        """Overview projection: server files nothing could answer for → 'unknown'.

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
            firmware_folder_verdicts=FakeFolderVerdicts(),
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
        _set_loop(fw, asyncio.get_event_loop())

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
        """Seeding every synced platform would fill the page with 0/0 rows.

        The core the page can ask is offered AND read, so the empty list is a
        finished answer — the one shape that may be dropped.
        """
        _seed_rom(plugin._uow, rom_id=44, platform_slug="nes", app_id=1)
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_resolver=FakeFirmwareResolver(),
            firmware_folder_verdicts=FakeFolderVerdicts(),
            core_info=_test_core_info(),
        )
        _set_loop(fw, asyncio.get_event_loop())

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")):
            result = await fw.get_firmware_status()

        assert result["platforms"] == []

    @pytest.mark.asyncio
    async def test_a_synced_platform_nothing_could_answer_for_stays_on_the_page(self, plugin, tmp_path):
        """A platform ES-DE offers no libretro core for keeps its block, reading unknown.

        The counterpart to the drop above, and the reason the drop is conditional
        (#1660). ``ps3``'s only ES-DE entry is RPCS3, so there is no core to ask
        and the empty file list is silence rather than an answer. Dropping the
        block would say the system needs nothing — the exact claim the four-value
        vocabulary exists to refuse — over firmware RPCS3 will not boot without.
        """
        _seed_rom(plugin._uow, rom_id=45, platform_slug="ps3", app_id=1)
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_resolver=FakeFirmwareResolver(),
            firmware_folder_verdicts=FakeFolderVerdicts(),
            core_info=FakeCoreInfoProvider(options=[]),
        )
        _set_loop(fw, asyncio.get_event_loop())

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")):
            result = await fw.get_firmware_status()

        ps3 = next(p for p in result["platforms"] if p["platform_slug"] == "ps3")
        assert ps3["files"] == []
        assert ps3["bios_level"] == "unknown"
        assert ps3["required_count"] == 0
        assert ps3["server_count"] == 0

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
        _set_loop(fw, asyncio.get_event_loop())

        real_exists = fw._config.firmware_file_store.exists
        checked: list[str] = []

        def _tracking_exists(path):
            checked.append(path)
            return real_exists(path)

        fw._config.firmware_file_store.exists = _tracking_exists

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.get_firmware_status()

        plat = next(p for p in result["platforms"] if p["platform_slug"] == "dc")
        assert {f["file_name"] for f in plat["files"]} == {"good.bin"}
        real_bios = os.path.realpath(str(bios_dir))
        for path in checked:
            assert os.path.realpath(path).startswith(real_bios + os.sep)


class TestGetFirmwareStatusDeletableCount:
    """``get_firmware_status`` ships the count the Delete BIOS button stands on.

    The delete is authorised by the download record, so the button's number has
    to be a record count. ``local_count`` is the library's progress ratio over a
    different set and is wrong in both directions: it counts a hand-placed file
    that shares a server file's name and drops our own download the moment RomM
    stops listing it — the direction that used to hide the button entirely over
    files it could still remove.
    """

    @pytest.mark.asyncio
    async def test_the_delete_count_counts_records_not_library_files(self, plugin, tmp_path):
        """Both directions in one platform, so the two counts cannot coincide.

        ``IPL.bin`` and ``card.bin`` are in the library and on disk but were put
        there by hand; ``retired.bin`` we downloaded and RomM no longer offers.
        The library ratio sees the first two, the delete sees only the third.
        """
        bios_dir = tmp_path / "bios"
        ipl = os.path.join(str(bios_dir), "IPL.bin")
        card = os.path.join(str(bios_dir), "card.bin")
        retired = os.path.join(str(bios_dir), "retired.bin")
        store = FakeFirmwareFileStore({ipl: b"\x00" * 8, card: b"\x00" * 8, retired: b"\x00" * 8})
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_file_store=store,
            retrodeck_paths=FakeRetroDeckPaths(bios=str(bios_dir)),
            core_info=_test_core_info(),
        )
        _inline_executor(fw)
        _declare(fw, ("IPL.bin", "GameCube IPL", True))
        plugin._uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug="gc",
                file_name="retired.bin",
                file_path=retired,
                downloaded_at="2026-01-01T00:00:00+00:00",
                firmware_id=None,
            )
        )
        _stub_listing(
            fw,
            [
                {"id": 7, "file_name": "IPL.bin", "file_path": "bios/gc/IPL.bin", "file_size_bytes": 8, "md5_hash": ""},
                {
                    "id": 8,
                    "file_name": "card.bin",
                    "file_path": "bios/gc/card.bin",
                    "file_size_bytes": 8,
                    "md5_hash": "",
                },
            ],
        )

        result = await fw.get_firmware_status()
        plat = next(p for p in result["platforms"] if p["platform_slug"] == "gc")

        assert plat["local_count"] == 2
        assert plat["deletable_count"] == 1
        # And per row, which is what a row's own Delete button reads. `IPL.bin`
        # is the shape that matters: in the library, on disk, `downloaded: True`
        # — and not ours, so it must offer nothing. Deriving the field from
        # `downloaded` instead of from the records flips exactly this row, and
        # it is the shape that destroyed `codehandler.bin` on a real device.
        rows = {f["file_name"]: f for f in plat["files"]}
        assert rows["IPL.bin"]["downloaded"] is True
        assert rows["IPL.bin"]["deletable_count"] == 0
        assert rows["card.bin"]["deletable_count"] == 0
        # `retired.bin` has no row at all: nothing declares it and the library no
        # longer offers it. So the platform count and the rows legitimately
        # differ — the count is over records, the rows are what the pane has to
        # show, and only the platform-wide button can reach this one.
        assert "retired.bin" not in rows
        # And the count means the delete: it removes ours, leaves theirs.
        deleted = await fw.delete_platform_bios("gc")
        assert deleted["deleted_count"] == 1
        assert retired not in store.files
        assert ipl in store.files
        assert card in store.files

    @pytest.mark.asyncio
    async def test_a_record_whose_file_is_gone_is_not_offered(self, plugin, tmp_path):
        """Nothing to unlink, nothing to offer — the button must not promise a deletion."""
        bios_dir = tmp_path / "bios"
        held = os.path.join(str(bios_dir), "IPL.bin")
        store = FakeFirmwareFileStore({held: b"\x00" * 8})
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_file_store=store,
            retrodeck_paths=FakeRetroDeckPaths(bios=str(bios_dir)),
            core_info=_test_core_info(),
        )
        _inline_executor(fw)
        _declare(fw, ("IPL.bin", "GameCube IPL", True))
        plugin._uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug="gc",
                file_name="gone.bin",
                file_path=os.path.join(str(bios_dir), "gone.bin"),
                downloaded_at="2026-01-01T00:00:00+00:00",
                firmware_id=None,
            )
        )
        _stub_listing(
            fw,
            [{"id": 7, "file_name": "IPL.bin", "file_path": "bios/gc/IPL.bin", "file_size_bytes": 8, "md5_hash": ""}],
        )

        result = await fw.get_firmware_status()
        plat = next(p for p in result["platforms"] if p["platform_slug"] == "gc")

        assert plat["local_count"] == 1
        assert plat["deletable_count"] == 0


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
            firmware_folder_verdicts=FakeFolderVerdicts(),
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

        fw._demand._retrodeck_paths = FakeRetroDeckPaths(bios=str(bios_dir))
        _set_loop(fw, asyncio.get_event_loop())

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

        _set_loop(fw, asyncio.get_event_loop())

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

        fw._demand._retrodeck_paths = FakeRetroDeckPaths(bios=str(bios_dir))
        _set_loop(fw, asyncio.get_event_loop())

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

        _set_loop(fw, asyncio.get_event_loop())

        download_called_ids = []

        async def fake_download_firmware(fw_id, _placements):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw._downloads, "_download_one", side_effect=fake_download_firmware),
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.download_all_firmware("dc")

        assert result["success"] is True
        assert result["downloaded"] == 1
        assert 2 in download_called_ids
        assert 1 not in download_called_ids

    @pytest.mark.asyncio
    async def test_a_folder_declaration_is_never_fetched(self, plugin, fw, tmp_path):
        """The emulator lists that name, so there is no file to fetch into it.

        The folder is ABSENT here, which is the case the already-there skip
        would let through: a download would write a file where the core opens a
        directory, and stage its ``.tmp`` beside the BIOS root.
        """
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        firmware_list = [
            {"id": 3, "file_name": "bios", "file_path": "bios/ps2/bios", "file_size_bytes": 0, "md5_hash": ""}
        ]
        resolver = _resolver(fw)
        resolver.declare(
            "bios",
            required_by=["pcsx2_libretro"],
            relative_path="pcsx2/bios",
            present=False,
            declares_directory=True,
            folder=FolderVerdict(satisfied=False),
        )
        _set_loop(fw, asyncio.get_event_loop())
        download_called_ids = []

        async def fake_download_firmware(fw_id, _placements):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw._downloads, "_download_one", side_effect=fake_download_firmware),
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.download_all_firmware("ps2")

        assert download_called_ids == []
        assert result["downloaded"] == 0


class TestDownloadPlatformFirmwareFile:
    """The per-row Download button (#164): one named file, scoped to its platform."""

    @staticmethod
    def _listing() -> list[dict[str, Any]]:
        return [
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
            {
                "id": 3,
                "file_name": "missing.bin",
                "file_path": "bios/n64/missing.bin",
                "file_size_bytes": 100,
                "md5_hash": "",
            },
        ]

    @pytest.mark.asyncio
    async def test_downloads_the_named_file_of_that_platform(self, plugin, fw, tmp_path):
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        _set_loop(fw, asyncio.get_event_loop())
        fetched = []

        async def fake_download_one(fw_id, _placements):
            fetched.append(fw_id)
            return {"success": True, "file_path": "/bios/dc/missing.bin", "md5_match": None}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=self._listing()),
            patch.object(fw._downloads, "_download_one", side_effect=fake_download_one),
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.download_platform_firmware_file("dc", "missing.bin")

        assert result["success"] is True
        assert result["downloaded"] == 1
        # id 3 carries the same file name under another platform's folder.
        assert fetched == [2]

    @pytest.mark.asyncio
    async def test_a_file_already_at_its_destination_is_not_refetched(self, plugin, fw, tmp_path):
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        (bios_dir / "existing.bin").write_bytes(b"\x00" * 50)
        _set_loop(fw, asyncio.get_event_loop())

        async def fake_download_one(fw_id, _placements):
            raise AssertionError(f"nothing to fetch, but firmware {fw_id} was requested")

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=self._listing()),
            patch.object(fw._downloads, "_download_one", side_effect=fake_download_one),
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.download_platform_firmware_file("dc", "existing.bin")

        assert result["success"] is True
        assert result["downloaded"] == 0
        assert "already here" in result["message"]

    @pytest.mark.asyncio
    async def test_a_name_the_platform_does_not_hold_is_refused(self, plugin, fw, tmp_path):
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        _set_loop(fw, asyncio.get_event_loop())

        async def fake_download_one(fw_id, _placements):
            raise AssertionError(f"nothing to fetch, but firmware {fw_id} was requested")

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=self._listing()),
            patch.object(fw._downloads, "_download_one", side_effect=fake_download_one),
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.download_platform_firmware_file("dc", "nowhere.bin")

        assert result["success"] is False
        assert result["reason"] == "not_in_library"
        assert result["downloaded"] == 0

    @pytest.mark.asyncio
    async def test_the_one_fetch_s_own_failure_is_surfaced(self, plugin, fw, tmp_path):
        # One press wants the reason: the single fetch's failure shape reaches
        # the caller intact rather than folded into a count of errors.
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        _set_loop(fw, asyncio.get_event_loop())

        async def fake_download_one(_fw_id, _placements):
            return {"success": False, "reason": "server_unreachable", "message": "RomM is unreachable"}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=self._listing()),
            patch.object(fw._downloads, "_download_one", side_effect=fake_download_one),
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.download_platform_firmware_file("dc", "missing.bin")

        assert result["success"] is False
        assert result["reason"] == "server_unreachable"
        assert result["message"] == "RomM is unreachable"
        assert result["downloaded"] == 0

    @pytest.mark.asyncio
    async def test_a_folder_declaration_is_refused_rather_than_fetched(self, plugin, fw, tmp_path):
        """The emulator opens that name as a directory, so there is no file to fetch.

        The folder is ABSENT here, which is the case the already-there skip would
        let through: a download would write a file where the core opens a
        directory, and stage its ``.tmp`` beside the BIOS root. Where the batch
        passes over such a row, one press asks for one named file, so this
        answers why instead of reporting a success nothing happened in.
        """
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        firmware_list = [
            {"id": 3, "file_name": "bios", "file_path": "bios/ps2/bios", "file_size_bytes": 0, "md5_hash": ""}
        ]
        _resolver(fw).declare(
            "bios",
            required_by=["pcsx2_libretro"],
            relative_path="pcsx2/bios",
            present=False,
            declares_directory=True,
            folder=FolderVerdict(satisfied=False),
        )
        _set_loop(fw, asyncio.get_event_loop())

        async def fake_download_one(fw_id, _placements):
            raise AssertionError(f"a folder declaration must not be fetched, but firmware {fw_id} was requested")

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw._downloads, "_download_one", side_effect=fake_download_one),
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
        ):
            result = await fw.download_platform_firmware_file("ps2", "bios")

        assert result["success"] is False
        assert result["reason"] == "declares_directory"
        assert result["downloaded"] == 0

    @pytest.mark.asyncio
    async def test_a_failed_listing_fetch_answers_with_zero(self, plugin, fw):
        _set_loop(fw, asyncio.get_event_loop())
        fw._listing._firmware_cache = None
        with patch.object(plugin._romm_api, "list_firmware", side_effect=OSError("Connection reset")):
            result = await fw.download_platform_firmware_file("dc", "missing.bin")

        assert result["success"] is False
        assert result["reason"] == "unknown"
        assert "Connection reset" in result["message"]
        assert result["downloaded"] == 0
        assert fw._listing._firmware_cache is None


class TestDeleteOneBiosFile:
    """The per-row Delete button's backend half — one file, the same authority."""

    @pytest.mark.asyncio
    async def test_deletes_our_own_download_at_the_recorded_path(self, plugin, fw, tmp_path):
        """The record names the file and the path, and both are what is used."""
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        ours = bios_dir / "gc-pal-12.bin"
        ours.write_bytes(b"\x00" * 512)
        plugin._uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug="gc",
                file_name="gc-pal-12.bin",
                file_path=str(ours),
                downloaded_at="2026-01-01T00:00:00+00:00",
                firmware_id=7,
            )
        )

        result = await fw.delete_bios_file("gc", "gc-pal-12.bin")

        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not ours.exists()
        assert plugin._uow.bios_files.get("gc", "gc-pal-12.bin") is None

    @pytest.mark.asyncio
    async def test_takes_only_the_named_record_of_several(self, plugin, fw, tmp_path):
        """One row's button removes one file, not the platform's other downloads.

        The platform-wide delete is the one that takes them all; this is the
        per-row twin and the name is the whole of the difference between them.
        """
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        for name in ("gc-pal-12.bin", "gc-ntsc-12.bin"):
            path = bios_dir / name
            path.write_bytes(b"\x00" * 32)
            plugin._uow.bios_files.save(
                BiosFile.mark_downloaded(
                    platform_slug="gc",
                    file_name=name,
                    file_path=str(path),
                    downloaded_at="2026-01-01T00:00:00+00:00",
                    firmware_id=1,
                )
            )

        result = await fw.delete_bios_file("gc", "gc-pal-12.bin")

        assert result["deleted_count"] == 1
        assert not (bios_dir / "gc-pal-12.bin").exists()
        assert (bios_dir / "gc-ntsc-12.bin").exists()
        assert plugin._uow.bios_files.get("gc", "gc-ntsc-12.bin") is not None

    @pytest.mark.asyncio
    async def test_deletes_our_downloads_inside_a_declared_folder(self, plugin, fw, tmp_path):
        """PS2's `pcsx2/bios` is a folder declaration, and what we put in it is ours.

        Two rules, not one: a folder is never offered as a DOWNLOAD, because
        there is no file to fetch into a name the emulator lists — that says
        nothing about the files already inside it. A recorded download under the
        folder must be removable, and a file we never placed must not be.
        """
        folder = tmp_path / "retrodeck" / "bios" / "pcsx2" / "bios"
        folder.mkdir(parents=True)
        ours = folder / "scph39001.bin"
        ours.write_bytes(b"\x00" * 128)
        theirs = folder / "scph70012.bin"
        theirs.write_bytes(b"\x01" * 128)
        plugin._uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug="ps2",
                file_name="scph39001.bin",
                file_path=str(ours),
                downloaded_at="2026-01-01T00:00:00+00:00",
                firmware_id=3,
            )
        )

        result = await fw.delete_bios_folder("ps2", str(folder))

        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not ours.exists()
        # The hand-placed file, and the folder itself, are not ours to remove.
        assert theirs.exists()
        assert folder.is_dir()

    @pytest.mark.asyncio
    async def test_a_folder_path_narrows_and_never_widens(self, plugin, fw, tmp_path):
        """A record outside the folder is out of reach of that folder's button."""
        bios = tmp_path / "retrodeck" / "bios"
        (bios / "pcsx2" / "bios").mkdir(parents=True)
        outside = bios / "scph5501.bin"
        outside.write_bytes(b"\x02" * 64)
        plugin._uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug="ps2",
                file_name="scph5501.bin",
                file_path=str(outside),
                downloaded_at="2026-01-01T00:00:00+00:00",
                firmware_id=4,
            )
        )

        result = await fw.delete_bios_folder("ps2", str(bios / "pcsx2" / "bios"))

        assert result["deleted_count"] == 0
        assert outside.exists()

    @pytest.mark.asyncio
    async def test_a_file_with_no_record_is_never_touched(self, plugin, fw, tmp_path):
        """The GameCube pane's other row: RetroDECK's own copy, present on disk.

        It sits one row above a real download, it is `downloaded: True`, and no
        RomM library can hand it back — authorising on presence deleted exactly
        this file on a real device. With no record naming it, the delete must
        remove nothing and report nothing removed.
        """
        sys_dir = tmp_path / "retrodeck" / "bios" / "dolphin-emu" / "Sys"
        sys_dir.mkdir(parents=True)
        theirs = sys_dir / "codehandler.bin"
        theirs.write_bytes(b"\x01" * 64)

        result = await fw.delete_bios_file("gc", "codehandler.bin")

        assert result["success"] is True
        assert result["deleted_count"] == 0
        assert theirs.exists()

    @pytest.mark.asyncio
    async def test_unlinks_where_the_record_points_not_where_the_row_would(self, plugin, fw, tmp_path):
        """A placement that moved after the download: the record still rules.

        The row's ``local_path`` is recomputed from today's placement, so for a
        file fetched before an emu-atlas bump it names whatever now occupies the
        new destination. Only the recorded path may be unlinked.
        """
        bios_dir = tmp_path / "retrodeck" / "bios"
        (bios_dir / "dc").mkdir(parents=True)
        written = bios_dir / "dc_boot.bin"
        written.write_bytes(b"\x02" * 16)
        elsewhere = bios_dir / "dc" / "dc_boot.bin"
        elsewhere.write_bytes(b"\x03" * 16)
        plugin._uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug="dc",
                file_name="dc_boot.bin",
                file_path=str(written),
                downloaded_at="2026-01-01T00:00:00+00:00",
                firmware_id=9,
            )
        )

        result = await fw.delete_bios_file("dc", "dc_boot.bin")

        assert result["deleted_count"] == 1
        assert not written.exists()
        assert elsewhere.exists()


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
                            declared_path="scph5501.bin",
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
        _set_loop(fw, asyncio.get_event_loop())
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

    @staticmethod
    def _gamecube_service(plugin, tmp_path):
        """A GameCube platform whose BIOS folder holds one of each kind of file.

        ``IPL.bin`` is in the RomM library; ``codehandler.bin`` is what RetroDECK
        ships beside its RetroArch component, wanted by the same core and held by
        no library. Both are declared, so both reach the delete as rows marked
        downloaded — which is the whole of what the old guard looked at.

        No download record is written here. Which file the plugin is supposed to
        have fetched is the axis under test, so each case states its own records
        (``_record_download``) — including the one that deliberately states none.
        """
        bios_dir = tmp_path / "bios"
        shipped = os.path.join(str(bios_dir), "dolphin-emu", "Sys", "codehandler.bin")
        ipl = os.path.join(str(bios_dir), "IPL.bin")
        store = FakeFirmwareFileStore({shipped: b"\x00" * 8, ipl: b"\x00" * 8})

        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_file_store=store,
            retrodeck_paths=FakeRetroDeckPaths(bios=str(bios_dir)),
            core_info=_test_core_info(),
        )
        _set_loop(fw, asyncio.get_event_loop())
        _resolver(fw).declare("IPL.bin", required_by=[_TEST_CORE], description="GameCube IPL")
        _resolver(fw).declare(
            "codehandler.bin",
            optional_for=[_TEST_CORE],
            relative_path="dolphin-emu/Sys/codehandler.bin",
            description="Dolphin code handler",
        )
        return fw, store, shipped, ipl

    @staticmethod
    def _gamecube_listing() -> list[dict[str, Any]]:
        return [
            {
                "id": 7,
                "file_name": "IPL.bin",
                "file_path": "bios/gc/IPL.bin",
                "file_size_bytes": 8,
                "md5_hash": "",
            },
        ]

    @staticmethod
    def _record_download(plugin, file_name: str, file_path: str) -> None:
        plugin._uow.bios_files.save(
            BiosFile.mark_downloaded(
                platform_slug="gc",
                file_name=file_name,
                file_path=file_path,
                downloaded_at="2026-01-01T00:00:00+00:00",
                firmware_id=7,
            )
        )

    @pytest.mark.asyncio
    async def test_an_emulator_shipped_file_survives_the_delete(self, plugin, tmp_path):
        """Deleting a platform's BIOS never touches a file the plugin did not fetch.

        The reported data loss: pressing Delete BIOS on GameCube removed
        ``dolphin-emu/Sys/codehandler.bin``, which RetroDECK ships with its own
        RetroArch component. It is on the list because an installed core asks for
        it, and its ``downloaded`` flag is nothing but ``os.path.exists`` — so the
        old guard, which read that flag alone, deleted a file nothing here could
        ever fetch back.
        """
        fw, store, shipped, ipl = self._gamecube_service(plugin, tmp_path)
        self._record_download(plugin, "IPL.bin", ipl)

        with patch.object(plugin._romm_api, "list_firmware", return_value=self._gamecube_listing()):
            status: dict[str, Any] = await fw.check_platform_bios("gc")
            # Precondition: the shipped file reaches the delete looking deletable.
            row = next(f for f in status["files"] if f["file_name"] == "codehandler.bin")
            assert row["downloaded"] is True

            result = await fw.delete_platform_bios("gc")

        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert shipped in store.files
        # The plugin's own download still goes, record and all.
        assert ipl not in store.files
        assert plugin._uow.bios_files.get("gc", "IPL.bin") is None

    @pytest.mark.asyncio
    async def test_a_file_with_no_download_record_survives_the_delete(self, plugin, tmp_path):
        """A hand-placed file is not the plugin's to delete, even under a server name.

        This file IS in the RomM library, so it looks like every other
        downloadable row and a guard reading the library would remove it.
        Nothing here put it on disk, so nothing here removes it.
        """
        fw, store, shipped, ipl = self._gamecube_service(plugin, tmp_path)

        with patch.object(plugin._romm_api, "list_firmware", return_value=self._gamecube_listing()):
            status: dict[str, Any] = await fw.check_platform_bios("gc")
            row = next(f for f in status["files"] if f["file_name"] == "IPL.bin")
            assert row["downloaded"] is True
            assert row["on_server"] is True

            result = await fw.delete_platform_bios("gc")

        assert result["success"] is True
        assert result["deleted_count"] == 0
        assert ipl in store.files
        assert shipped in store.files

    @pytest.mark.asyncio
    async def test_our_own_download_still_goes_after_it_leaves_the_library(self, plugin, tmp_path):
        """A file we downloaded stays deletable once RomM no longer holds it.

        The library is not the authority here — the record is. Dropping a
        firmware file from RomM flips its row to ``on_server: False``, and a
        guard that also demanded ``on_server`` would strand our own download on
        disk with nothing in the UI able to remove it. The row is otherwise
        indistinguishable from the emulator-shipped one beside it; only the
        record tells them apart, and only the record decides.
        """
        fw, store, shipped, ipl = self._gamecube_service(plugin, tmp_path)
        self._record_download(plugin, "IPL.bin", ipl)

        with patch.object(plugin._romm_api, "list_firmware", return_value=[]):
            status: dict[str, Any] = await fw.check_platform_bios("gc")
            # Both rows now come off the machine's demand, not the listing.
            assert {f["file_name"]: f["on_server"] for f in status["files"]} == {
                "IPL.bin": False,
                "codehandler.bin": False,
            }

            result = await fw.delete_platform_bios("gc")

        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert ipl not in store.files
        assert plugin._uow.bios_files.get("gc", "IPL.bin") is None
        # The one without a record is still not ours.
        assert shipped in store.files

    @pytest.mark.asyncio
    async def test_a_moved_placement_does_not_redirect_the_delete(self, plugin, tmp_path):
        """The delete unlinks where the download wrote, never where the placement now points.

        ``codehandler.bin`` was fetched while nothing declared it, so it landed
        flat in the BIOS root and the record says so. A later emu-atlas bump
        gives the file a subdirectory — the very path RetroDECK's own copy
        occupies. A delete that recomputed the destination would still match our
        record by name, unlink RetroDECK's file, drop the row, and leave ours on
        disk with nothing left that could remove it.
        """
        fw, store, shipped, _ipl = self._gamecube_service(plugin, tmp_path)
        flat = os.path.join(str(tmp_path / "bios"), "codehandler.bin")
        store.files[flat] = b"\x00" * 8
        self._record_download(plugin, "codehandler.bin", flat)

        with patch.object(plugin._romm_api, "list_firmware", return_value=self._gamecube_listing()):
            status: dict[str, Any] = await fw.check_platform_bios("gc")
            # Precondition: today's placement puts the row on the shipped copy.
            row = next(f for f in status["files"] if f["file_name"] == "codehandler.bin")
            assert row["local_path"] == shipped

            result = await fw.delete_platform_bios("gc")

        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert flat not in store.files
        assert shipped in store.files
        assert plugin._uow.bios_files.get("gc", "codehandler.bin") is None

    @pytest.mark.asyncio
    async def test_a_record_whose_file_is_gone_is_pruned_without_a_deletion(self, plugin, tmp_path):
        """A row standing over an absent file is dropped, counted as nothing, reported as no error.

        The user deleted it by hand, or a previous run took it. There is nothing
        to unlink, and leaving the row would keep offering a file that is not
        there.
        """
        fw, store, shipped, ipl = self._gamecube_service(plugin, tmp_path)
        self._record_download(plugin, "IPL.bin", ipl)
        del store.files[ipl]

        with patch.object(plugin._romm_api, "list_firmware", return_value=self._gamecube_listing()):
            result = await fw.delete_platform_bios("gc")

        assert result["success"] is True
        assert result["deleted_count"] == 0
        assert plugin._uow.bios_files.get("gc", "IPL.bin") is None
        assert shipped in store.files

    @pytest.mark.asyncio
    async def test_two_records_for_one_file_are_one_deletion(self, plugin, tmp_path):
        """A platform holding the same file under two firmware slugs deletes it once.

        ``psx`` reads both ``psx`` and ``ps``. Two rows naming one path is one
        unlink: the first takes the file, the second finds it gone and prunes
        over the absence, so neither the count nor the error list doubles.
        """
        bios_dir = tmp_path / "bios"
        path = os.path.join(str(bios_dir), "scph5501.bin")
        store = FakeFirmwareFileStore({path: b"\x00" * 8})
        fw = _make_firmware_service(
            romm_api=plugin._romm_api,
            uow_factory=FakeUnitOfWorkFactory(plugin._uow),
            firmware_file_store=store,
            retrodeck_paths=FakeRetroDeckPaths(bios=str(bios_dir)),
            core_info=_test_core_info(),
        )
        _set_loop(fw, asyncio.get_event_loop())
        for slug in ("psx", "ps"):
            plugin._uow.bios_files.save(
                BiosFile.mark_downloaded(
                    platform_slug=slug,
                    file_name="scph5501.bin",
                    file_path=path,
                    downloaded_at="2026-01-01T00:00:00+00:00",
                    firmware_id=1,
                )
            )

        with patch.object(plugin._romm_api, "list_firmware", return_value=[]):
            result = await fw.delete_platform_bios("psx")

        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert path not in store.files
        assert plugin._uow.bios_files.get("psx", "scph5501.bin") is None
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
                            declared_path="bios1.bin",
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
                            declared_path="bios2.bin",
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
        # Flat in the bios root: the declarations below name no subdirectory.
        bios_dir = tmp_path / "retrodeck" / "bios"
        bios_dir.mkdir(parents=True)
        (bios_dir / "required1.bin").write_bytes(b"\x00" * 100)
        (bios_dir / "required2.bin").write_bytes(b"\x00" * 200)
        _resolver_reads(fw, bios_dir)
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

        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
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
        _resolver_reads(fw, bios_dir)
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

        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))):
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
        reported a green "Nothing required" over firmware RPCS3 will not boot
        without.
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
        _set_loop(fw, asyncio.get_event_loop())

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

        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))):
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

        fw._config.core_info.active_core = (_TEST_CORE, "Test Core")
        _declare(fw, ("required.bin", "Required BIOS", True), ("optional.bin", "Optional firmware", False))

        _set_loop(fw, asyncio.get_event_loop())

        download_called_ids = []

        async def fake_download_firmware(fw_id, _placements):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw._downloads, "_download_one", side_effect=fake_download_firmware),
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

        with patch.object(fw._downloads, "_download_one", side_effect=fake_download_firmware):
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

        _set_loop(fw, asyncio.get_event_loop())

        download_called_ids = []

        async def fake_download_firmware(fw_id, _placements):
            download_called_ids.append(fw_id)
            return {"success": True}

        with (
            patch.object(plugin._romm_api, "list_firmware", return_value=firmware_list),
            patch.object(fw._downloads, "_download_one", side_effect=fake_download_firmware),
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(bios_dir))),
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
        _set_loop(fw, asyncio.get_event_loop())

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
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))),
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
        _set_loop(fw, asyncio.get_event_loop())

        with patch.object(plugin._romm_api, "list_firmware", side_effect=Exception("offline")):
            result = await fw.check_platform_bios("n64")

        assert result == {"needs_bios": False, "bios_status_unknown": True}

    @pytest.mark.asyncio
    async def test_a_cached_listing_still_contributes_its_own_files(self, plugin, fw, tmp_path):
        """The listing cache is what keeps the server-only rows available offline."""
        fw._listing._firmware_cache = [
            {
                "id": 1,
                "file_name": "scph5501.bin",
                "file_path": "bios/ps/scph5501.bin",
                "file_size_bytes": 512,
                "md5_hash": "",
            },
        ]
        fw._listing._firmware_cache_epoch = fw._config.clock.time()
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
            patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))),
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
        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))):
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
        fw._config.core_info.active_core = ("mgba_libretro", "mGBA")

        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))):
            result = await fw.check_platform_bios("gba", active_core_so="gpsp_libretro")

        assert result["needs_bios"] is True
        assert result["required_count"] == 1  # gpSP requires gba_bios.bin
        # The pre-resolved core short-circuits the system-default read entirely.
        assert fw._config.core_info.active_core_calls == []

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
        fw._config.core_info.active_core = ("mgba_libretro", "mGBA")

        with patch.object(fw._demand, "_retrodeck_paths", FakeRetroDeckPaths(bios=str(tmp_path / "bios"))):
            result = await fw.check_platform_bios("gba")

        assert result["needs_bios"] is True
        assert result["required_count"] == 0  # mGBA treats gba_bios.bin as optional
        # None → the system default was read once for the system "gba".
        assert fw._config.core_info.active_core_calls == ["gba"]


class TestDownloadFirmwareErrors:
    """Tests for download_firmware error handling."""

    @pytest.mark.asyncio
    async def test_fetch_metadata_error(self, fw):
        """Fetch firmware metadata failure returns error."""
        assert isinstance(fw._config.romm_api, MagicMock)
        fw._config.romm_api.get_firmware.side_effect = Exception("not found")
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

        fw._demand._retrodeck_paths = FakeRetroDeckPaths(bios=str(bios_dir))
        _set_loop(fw, asyncio.get_event_loop())

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

        result1 = fw._listing.get_firmware_list()
        result2 = fw._listing.get_firmware_list()

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

        result1 = fw._listing.get_firmware_list()
        assert len(result1) == 1
        assert api.list_firmware.call_count == 1

        # Simulate TTL expiry by backdating the wall-clock cache epoch
        fw._listing._firmware_cache_epoch = fw._config.clock.time() - 3601

        result2 = fw._listing.get_firmware_list()
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
        assert fw._listing._firmware_cache == [
            {
                "id": 1,
                "file_name": "bios.bin",
                "file_path": "bios/dc/bios.bin",
                "file_size_bytes": 2048,
                "md5_hash": "",
            }
        ]
        assert fw._listing._firmware_cache_epoch == stale_epoch

        result = fw._listing.get_firmware_list()

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

        fw._listing.get_firmware_list()
        assert api.list_firmware.call_count == 1

        fw.invalidate_firmware_cache()
        result = fw._listing.get_firmware_list()
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

        result1 = fw._listing.get_firmware_list()
        assert len(result1) == 1

        # Expire the cache so it tries to re-fetch (must be far enough in the past
        # to exceed TTL even when system uptime is short)
        fw._listing._firmware_cache_epoch = fw._config.clock.time() - 7200

        result2 = fw._listing.get_firmware_list()
        assert result2 == result1  # Falls back to stale cache
        assert api.list_firmware.call_count == 2

    def test_firmware_cache_error_no_cache_raises(self):
        """HTTP error with no prior cache re-raises so callers can detect offline."""
        api = MagicMock()
        api.list_firmware.side_effect = Exception("connection refused")
        fw = self._make_service(api)

        with pytest.raises(Exception, match="connection refused"):
            fw._listing.get_firmware_list()


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
        assert fw._listing._firmware_cache == [
            {
                "id": 1,
                "file_name": "bios.bin",
                "file_path": "bios/dc/bios.bin",
                "file_size_bytes": 512,
                "md5_hash": "",
            }
        ]
        assert fw._listing._firmware_cache_epoch == 1000.0

    def test_empty_db_cache_leaves_memory_none(self):
        """Empty DB cache doesn't populate the in-memory cache."""
        fw = _make_firmware_service(uow_factory=FakeUnitOfWorkFactory(FakeUnitOfWork()))
        assert fw._listing._firmware_cache is None

    def test_db_read_failure_handled_gracefully(self):
        """A repo error during restore doesn't crash init."""
        uow = FakeUnitOfWork()
        with patch.object(uow.firmware_cache, "iter_all", side_effect=OSError("db locked")):
            fw = _make_firmware_service(uow_factory=FakeUnitOfWorkFactory(uow))
        assert fw._listing._firmware_cache is None

    def test_cache_persisted_after_http_fetch(self, plugin, fw):
        """Firmware cache written to the DB after a successful HTTP fetch."""
        firmware_list = [{"id": 1, "file_name": "bios.bin", "file_path": "bios/dc/bios.bin", "file_size_bytes": 512}]
        _stub_listing(fw, firmware_list)
        fw._listing._firmware_cache = None  # Force refetch

        result = fw._listing.get_firmware_list()

        assert result == firmware_list
        assert plugin._uow.firmware_cache.replace_count == 1
        # The thin aggregate carries the parsed slug ("dc") and name.
        stored = plugin._uow.firmware_cache.get("dc", "bios.bin")
        assert stored is not None
        assert stored.id == 1
        assert stored.file_size_bytes == 512
        assert stored.cached_at == fw._listing._firmware_cache_epoch

    def test_invalidate_clears_persisted_cache(self, plugin, fw):
        """invalidate_firmware_cache drops every DB cache row."""
        _seed_firmware_cache(
            plugin._uow,
            [FirmwareCacheEntry.cached(id=1, name="x.bin", platform_slug="dc", file_size_bytes=10, cached_at=1.0)],
        )
        fw._listing._firmware_cache = [{"id": 1}]
        fw._listing._firmware_cache_epoch = 1.0

        fw.invalidate_firmware_cache()

        assert fw._listing._firmware_cache is None
        assert list(plugin._uow.firmware_cache.iter_all()) == []

    def test_persist_failure_does_not_crash_fetch(self, plugin, fw):
        """A DB write failure during fetch doesn't break the return value."""
        firmware_list = [{"id": 1, "file_name": "bios.bin", "file_path": "bios/dc/bios.bin", "file_size_bytes": 512}]
        _stub_listing(fw, firmware_list)
        fw._listing._firmware_cache = None

        with patch.object(plugin._uow.firmware_cache, "replace_all", side_effect=OSError("disk full")):
            result = fw._listing.get_firmware_list()

        assert result == firmware_list
        assert fw._listing._firmware_cache == firmware_list


class TestDeletePlatformBiosIOLogsWarnings:
    """Coverage for the OSError-warning path in _delete_platform_bios_io."""

    @pytest.mark.asyncio
    async def test_logs_warning_and_collects_error_when_remove_fails(self, plugin, fw, caplog):
        """A per-file OSError surfaces as a logger.warning and an error entry."""
        import logging

        fake_files = FakeFirmwareFileStore({"/fake/bios/scph5501.bin": b"\x00", "/fake/bios/scph5502.bin": b"\x00"})
        fake_files.remove_failures.add("/fake/bios/scph5501.bin")
        fw._deletion._firmware_file_store = fake_files

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
        fw._listing._firmware_cache = [{"id": 1, "file_name": "bios.bin"}]
        fw._listing._firmware_cache_epoch = 1.0

        with (
            patch.object(uow.firmware_cache, "clear", side_effect=OSError("disk full")),
            caplog.at_level(logging.WARNING),
        ):
            fw.invalidate_firmware_cache()  # must not raise

        # In-memory cache cleared regardless of DB failure.
        assert fw._listing._firmware_cache is None
        assert fw._listing._firmware_cache_epoch == 0
        # The failure surfaced as a warning.
        assert any("disk full" in record.getMessage() for record in caplog.records)

    @pytest.mark.asyncio
    async def test_download_all_firmware_returns_error_with_zero_when_list_fetch_fails(self, fake_romm_api, caplog):
        """Initial ``list_firmware`` failure short-circuits with ``downloaded=0``."""
        import logging

        fw = self._build_service(fake_romm_api)
        _set_loop(fw, asyncio.get_event_loop())
        fake_romm_api.fail_on_next(OSError("connection reset"))

        with caplog.at_level(logging.ERROR):
            result = await fw.download_all_firmware("dc")

        assert result["success"] is False
        assert result["downloaded"] == 0
        assert "message" in result
        # The cache was not populated by the failed fetch.
        assert fw._listing._firmware_cache is None

    @pytest.mark.asyncio
    async def test_download_required_firmware_returns_error_with_zero_when_list_fetch_fails(
        self, fake_romm_api, caplog
    ):
        """Initial ``list_firmware`` failure short-circuits with ``downloaded=0``."""
        import logging

        fw = self._build_service(fake_romm_api)
        _set_loop(fw, asyncio.get_event_loop())
        fake_romm_api.fail_on_next(OSError("connection reset"))

        with caplog.at_level(logging.ERROR):
            result = await fw.download_required_firmware("dc")

        assert result["success"] is False
        assert result["downloaded"] == 0
        assert "message" in result
        # The cache was not populated by the failed fetch.
        assert fw._listing._firmware_cache is None
