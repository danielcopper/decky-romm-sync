"""RomInstallRecorder — the launchable verdict, the row, and the size write-back.

The recorder is the one path both a completed download and an adoption reach an
install through, so these cases hold for both.
"""

import logging

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock

from domain.rom import Rom
from services.rom_install_recorder import RomInstallRecorder, RomInstallRecorderConfig


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


class _Harness:
    """The recorder plus the state its tests inspect afterwards."""

    def __init__(self) -> None:
        self._uow = FakeUnitOfWork()
        # Empty by default ("ES-DE could not answer"), which the launch-target
        # check treats as launchable; a test that exercises the check seeds a
        # real per-system accept-list here.
        self._system_extensions: dict[str, frozenset[str]] = {}
        self._install_recorder = RomInstallRecorder(
            config=RomInstallRecorderConfig(
                logger=logging.getLogger("test_rom_install_recorder"),
                clock=FakeClock(),
                uow_factory=FakeUnitOfWorkFactory(self._uow),
                system_extensions=lambda system_name: self._system_extensions.get(system_name, frozenset()),
                active_core=FakeActiveCoreResolver(default=(None, None)),
                disc_resolver=FakeDiscResolver(),
            ),
        )


@pytest.fixture
def plugin():
    return _Harness()


class TestRecordInstallLaunchTarget:
    """The launch-target verdict recorded at install time (#1652).

    The accept-lists seeded here are ES-DE's real per-system ``<extension>``
    sets, so a passing case is not a tautology over a made-up list.
    """

    _PS3 = frozenset({".desktop", ".iso", ".ps3", ".ps3dir"})
    _DREAMCAST = frozenset({".cdi", ".chd", ".cue", ".dat", ".elf", ".gdi", ".iso", ".lst", ".m3u", ".7z", ".zip"})

    def _record(self, plugin, *, file_path, rom_dir, system, cleanup=lambda: None):
        _seed_rom(plugin._uow, 42)
        return plugin._install_recorder.do_record_install(
            rom_id=42,
            rom_detail={"platform_slug": system},
            file_path=file_path,
            rom_dir=rom_dir,
            system=system,
            cleanup=cleanup,
        )

    def test_ps3_pkg_records_an_unlaunchable_install_and_keeps_the_files(self, plugin):
        # The reported case (#1582). The row is written, the install is NOT
        # refused, and cleanup is NEVER called — the package stays on disk so the
        # user can install it by hand in RPCS3.
        plugin._system_extensions = {"ps3": self._PS3}
        cleanup_calls = []

        file_path, error = self._record(
            plugin,
            file_path="/roms/ps3/Puppeteer/Puppeteer.pkg",
            rom_dir="/roms/ps3/Puppeteer",
            system="ps3",
            cleanup=lambda: cleanup_calls.append(1),
        )

        assert error is None
        assert file_path == "/roms/ps3/Puppeteer/Puppeteer.pkg"
        assert cleanup_calls == []
        install = plugin._uow.rom_installs.get(42)
        assert install is not None
        assert install.launchable is False
        assert install.file_path == "/roms/ps3/Puppeteer/Puppeteer.pkg"
        assert install.rom_dir == "/roms/ps3/Puppeteer"

    def test_dreamcast_track_bin_records_an_unlaunchable_install(self, plugin):
        # A multi-file GDI rip with no .cue: the fallback picks the largest track
        # file, and dreamcast's accept-list carries no .bin.
        plugin._system_extensions = {"dreamcast": self._DREAMCAST}

        _, error = self._record(
            plugin, file_path="/roms/dc/Game/track03.bin", rom_dir="/roms/dc/Game", system="dreamcast"
        )

        assert error is None
        install = plugin._uow.rom_installs.get(42)
        assert install is not None
        assert install.launchable is False

    def test_ps3_folder_boot_dump_stays_launchable(self, plugin):
        # The carve-out that protects every working PS3 dump: file_path records
        # the nested EBOOT (a .bin, absent from ps3's list) but the bake target
        # is the game directory, which ES-DE spells .ps3dir (ADR-0019).
        plugin._system_extensions = {"ps3": self._PS3}

        _, error = self._record(
            plugin,
            file_path="/roms/ps3/MyGame/PS3_GAME/USRDIR/EBOOT.BIN",
            rom_dir="/roms/ps3/MyGame",
            system="ps3",
        )

        assert error is None
        install = plugin._uow.rom_installs.get(42)
        assert install is not None
        assert install.launchable is True

    def test_ps3_folder_boot_dump_still_bakes_the_game_directory_end_to_end(self, plugin):
        # The one shape that can silently break a working install, walked end to
        # end: record the install through the real check, then resolve the
        # persisted row through the REAL DiscLaunchResolver the bake sites use.
        # A False verdict anywhere in that chain collapses the bake path to ""
        # and the PS3 dump loses the launch command it has today. No installed
        # ROM in a typical library has this shape, so only this fixture pins it.
        from services.disc_launch_resolver import DiscLaunchResolver, DiscLaunchResolverConfig

        plugin._system_extensions = {"ps3": self._PS3}
        rom_dir = "/roms/ps3/MyGame"
        eboot = f"{rom_dir}/PS3_GAME/USRDIR/EBOOT.BIN"

        self._record(plugin, file_path=eboot, rom_dir=rom_dir, system="ps3")

        install = plugin._uow.rom_installs.get(42)
        assert install is not None
        assert install.launchable is True
        resolver = DiscLaunchResolver(
            config=DiscLaunchResolverConfig(
                list_files=lambda directory: [eboot] if directory == rom_dir else [],
                system_extensions=lambda system_name: plugin._system_extensions.get(system_name, frozenset()),
                logger=logging.getLogger("test_rom_install_recorder"),
            ),
        )
        assert resolver.resolve_for_install(install, None) == rom_dir

    def test_desktop_entry_stays_launchable(self, plugin):
        plugin._system_extensions = {"ps3": self._PS3}

        _, error = self._record(plugin, file_path="/roms/ps3/Game.desktop", rom_dir=None, system="ps3")

        assert error is None
        install = plugin._uow.rom_installs.get(42)
        assert install is not None
        assert install.launchable is True

    def test_unknown_system_stays_launchable(self, plugin):
        # ES-DE could not answer (empty accept-list). A missing answer must never
        # turn a working install into an unlaunchable one.
        plugin._system_extensions = {}

        _, error = self._record(
            plugin, file_path="/roms/ps3/Puppeteer/Puppeteer.pkg", rom_dir="/roms/ps3/Puppeteer", system="ps3"
        )

        assert error is None
        install = plugin._uow.rom_installs.get(42)
        assert install is not None
        assert install.launchable is True

    def test_unlaunchable_install_is_logged(self, plugin, caplog):
        plugin._system_extensions = {"ps3": self._PS3}

        with caplog.at_level(logging.WARNING):
            self._record(
                plugin, file_path="/roms/ps3/Puppeteer/Puppeteer.pkg", rom_dir="/roms/ps3/Puppeteer", system="ps3"
            )

        assert any("No launch target for rom_id=42" in r.message for r in caplog.records)

    def test_launchable_install_logs_nothing(self, plugin, caplog):
        plugin._system_extensions = {"ps3": self._PS3}

        with caplog.at_level(logging.WARNING):
            self._record(plugin, file_path="/roms/ps3/Game.iso", rom_dir=None, system="ps3")

        assert not any("No launch target" in r.message for r in caplog.records)


class TestRecordInstallFsSizeWriteBack:
    """A completed install tops up ``roms.fs_size_bytes`` from the ROM detail (#1395).

    The between-syncs freshness write-back: guarded on truthiness so a
    missing/zero server size never clobbers a good persisted value.
    """

    def test_successful_install_stamps_server_size(self, plugin):
        uow = plugin._uow
        _seed_rom(uow, 42)

        file_path, error = plugin._install_recorder.do_record_install(
            rom_id=42,
            rom_detail={"platform_slug": "n64", "fs_size_bytes": 3_145_728},
            file_path="/roms/n64/game_42.z64",
            rom_dir=None,
            system="n64",
            cleanup=lambda: None,
        )

        assert error is None
        assert file_path == "/roms/n64/game_42.z64"
        rom = uow.roms.get(42)
        assert rom is not None
        assert rom.fs_size_bytes == 3_145_728
        # The install itself still persisted in the same UoW.
        assert uow.rom_installs.get(42) is not None

    def test_missing_fs_size_bytes_does_not_overwrite(self, plugin):
        # The guard protects a good persisted value when the detail omits the size.
        uow = plugin._uow
        seeded = Rom.synced(
            rom_id=42,
            platform_slug="n64",
            name="Game 42",
            fs_name="game_42.z64",
            shortcut_app_id=1042,
            synced_at="2026-01-01T00:00:00+00:00",
            fs_size_bytes=999_000,
        )
        uow.roms.save(seeded)

        _, error = plugin._install_recorder.do_record_install(
            rom_id=42,
            rom_detail={"platform_slug": "n64"},  # no fs_size_bytes key
            file_path="/roms/n64/game_42.z64",
            rom_dir=None,
            system="n64",
            cleanup=lambda: None,
        )

        assert error is None
        rom = uow.roms.get(42)
        assert rom is not None
        assert rom.fs_size_bytes == 999_000

    def test_zero_fs_size_bytes_does_not_overwrite(self, plugin):
        # A zero size is falsy — the guard skips the write, preserving the value.
        uow = plugin._uow
        seeded = Rom.synced(
            rom_id=42,
            platform_slug="n64",
            name="Game 42",
            fs_name="game_42.z64",
            shortcut_app_id=1042,
            synced_at="2026-01-01T00:00:00+00:00",
            fs_size_bytes=999_000,
        )
        uow.roms.save(seeded)

        _, error = plugin._install_recorder.do_record_install(
            rom_id=42,
            rom_detail={"platform_slug": "n64", "fs_size_bytes": 0},
            file_path="/roms/n64/game_42.z64",
            rom_dir=None,
            system="n64",
            cleanup=lambda: None,
        )

        assert error is None
        rom = uow.roms.get(42)
        assert rom is not None
        assert rom.fs_size_bytes == 999_000
