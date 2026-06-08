"""Tests for CoreService — system core write + per-game pin/clear + core menu."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.rom import Rom
from domain.rom_install import RomInstall
from services.cores import CoreService, CoreServiceConfig


class FakeGamelistEditor:
    """In-memory ``GamelistXmlEditor`` for tests."""

    def __init__(self) -> None:
        self.system_calls: list[tuple[str, str, str | None]] = []
        self.system_side_effect: BaseException | None = None

    def set_system_override(self, retrodeck_home: str, system_name: str, core_label: str | None) -> bool:
        if self.system_side_effect is not None:
            raise self.system_side_effect
        self.system_calls.append((retrodeck_home, system_name, core_label))
        return True


class FakeSystemResolver:
    """In-memory ``SystemResolver`` for tests.

    Maps known RomM platform slugs to RetroDECK systems and records each
    call so tests can assert resolution happened. Unknown slugs fall
    through unchanged, mirroring the real resolver's pass-through.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {}
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, platform_slug: str, platform_fs_slug: str | None = None) -> str:
        self.calls.append((platform_slug, platform_fs_slug))
        return self.mapping.get(platform_slug, platform_slug)


class FakeBiosChecker:
    """In-memory ``BiosChecker`` for tests (only implements the async entry CoreService uses)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.payload: dict[str, Any] = {"needs_bios": False}
        self.side_effect: BaseException | None = None

    async def check_platform_bios(self, platform_slug: str, active_core_so: str | None = None) -> dict[str, Any]:
        if self.side_effect is not None:
            raise self.side_effect
        self.calls.append((platform_slug, active_core_so))
        return self.payload


def _seed_rom(
    uow: FakeUnitOfWork,
    *,
    rom_id: int,
    platform_slug: str = "snes",
    shortcut_app_id: int | None = None,
    emulator_override: str | None = None,
) -> None:
    uow.roms.save(
        Rom(
            rom_id=rom_id,
            platform_slug=platform_slug,
            name=f"rom-{rom_id}",
            fs_name=f"rom-{rom_id}.sfc",
            shortcut_app_id=shortcut_app_id,
            last_synced_at="2026-01-01T00:00:00+00:00",
            emulator_override=emulator_override,
        )
    )


def _seed_install(uow: FakeUnitOfWork, *, rom_id: int, file_path: str, platform_slug: str = "snes") -> None:
    uow.rom_installs.save(
        RomInstall(
            rom_id=rom_id,
            file_path=file_path,
            rom_dir=None,
            platform_slug=platform_slug,
            system=platform_slug,
            installed_at="2026-01-01T00:00:00+00:00",
        )
    )


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_cores")


@pytest.fixture
def core_info() -> FakeCoreInfoProvider:
    return FakeCoreInfoProvider(
        active_core=("snes9x_libretro", "Snes9x"),
        available_cores=[
            {"core_so": "snes9x_libretro", "label": "Snes9x", "is_default": True},
            {"core_so": "bsnes_libretro", "label": "bsnes", "is_default": False},
        ],
    )


@pytest.fixture
def gamelist_editor() -> FakeGamelistEditor:
    return FakeGamelistEditor()


@pytest.fixture
def resolve_system() -> FakeSystemResolver:
    return FakeSystemResolver(
        mapping={
            "dc": "dreamcast",
            "sms": "mastersystem",
            "neo-geo-pocket": "ngp",
        }
    )


@pytest.fixture
def bios_checker() -> FakeBiosChecker:
    return FakeBiosChecker()


@pytest.fixture
def retrodeck_paths() -> FakeRetroDeckPaths:
    return FakeRetroDeckPaths(home="/home/deck/retrodeck")


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def uow_factory(uow) -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory(uow=uow)


@pytest.fixture
def active_core() -> FakeActiveCoreResolver:
    return FakeActiveCoreResolver(default=("snes9x_libretro", "Snes9x"))


@pytest.fixture
def service(
    event_loop,
    logger,
    core_info,
    gamelist_editor,
    resolve_system,
    bios_checker,
    retrodeck_paths,
    uow_factory,
    active_core,
) -> CoreService:
    return CoreService(
        config=CoreServiceConfig(
            loop=event_loop,
            logger=logger,
            core_info=core_info,
            gamelist_editor=gamelist_editor,
            resolve_system=resolve_system,
            retrodeck_paths=retrodeck_paths,
            bios_checker=bios_checker,
            uow_factory=uow_factory,
            active_core=active_core,
        ),
    )


# ── get_available_cores (rom_id-keyed core menu) ───────────────────────


class TestGetAvailableCores:
    def test_happy_path(self, event_loop, service, core_info, uow):
        _seed_rom(uow, rom_id=42, platform_slug="snes")
        result = event_loop.run_until_complete(service.get_available_cores(42))
        assert result == {
            "cores": core_info.available_cores,
            "active_core": "snes9x_libretro",
            "active_core_label": "Snes9x",
        }

    def test_unknown_rom_returns_empty(self, event_loop, service, core_info):
        # No ROM seeded for rom_id=7 → empty cores + no active core, and the
        # platform-wide core enumeration is never reached.
        result = event_loop.run_until_complete(service.get_available_cores(7))
        assert result == {"cores": [], "active_core": None, "active_core_label": None}
        assert core_info.available_cores_calls == []

    def test_active_marker_reflects_pin(self, event_loop, service, uow, active_core):
        # A pinned ROM surfaces the OVERRIDE core as active (via the resolver),
        # not the system default — the menu highlights the pin.
        _seed_rom(uow, rom_id=42, platform_slug="snes", emulator_override="bsnes")
        active_core.per_rom[42] = ("bsnes_libretro", "bsnes")
        result = event_loop.run_until_complete(service.get_available_cores(42))
        assert result["active_core"] == "bsnes_libretro"
        assert result["active_core_label"] == "bsnes"
        assert active_core.calls == [42]

    def test_active_marker_falls_back_to_system_default(self, event_loop, service, uow, active_core):
        # An unpinned ROM (NULL override) surfaces the system default via the
        # resolver — the same seam, no divergence from the launched core.
        _seed_rom(uow, rom_id=42, platform_slug="snes")
        active_core.default = ("snes9x_libretro", "Snes9x")
        result = event_loop.run_until_complete(service.get_available_cores(42))
        assert result["active_core"] == "snes9x_libretro"
        assert result["active_core_label"] == "Snes9x"

    def test_empty_cores_list(self, event_loop, service, core_info, uow):
        _seed_rom(uow, rom_id=42, platform_slug="snes")
        core_info.available_cores = []
        result = event_loop.run_until_complete(service.get_available_cores(42))
        assert result["cores"] == []

    @pytest.mark.parametrize(
        ("slug", "system"),
        [
            ("dc", "dreamcast"),
            ("sms", "mastersystem"),
            ("neo-geo-pocket", "ngp"),
            ("snes", "snes"),  # identity: slug already equals system
        ],
    )
    def test_resolves_system_for_available_cores(
        self, event_loop, service, core_info, resolve_system, uow, slug, system
    ):
        _seed_rom(uow, rom_id=42, platform_slug=slug)
        event_loop.run_until_complete(service.get_available_cores(42))
        # The platform-wide enumeration receives the NORMALIZED system.
        assert core_info.available_cores_calls == [system]
        assert resolve_system.calls == [(slug, None)]


# ── set_game_core (per-game pin; B4 hard-fail-before-write) ─────────────


class TestSetGameCore:
    def test_installed_and_bound_pins_and_returns_override_launch(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=99)
        _seed_install(uow, rom_id=42, file_path="/roms/snes/mario.sfc")
        result = event_loop.run_until_complete(service.set_game_core(42, "bsnes"))
        assert result["success"] is True
        assert result["app_id"] == 99
        # The -e override form bakes the resolved core for the pinned label. The
        # available-cores map keys on the BARE core name (bsnes_libretro); the
        # bake appends exactly one ".so" for the on-disk RetroArch core path.
        assert result["launch_options"] == (
            "flatpak run net.retrodeck.retrodeck -e "
            '"%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/bsnes_libretro.so %ROM%" '
            '"/roms/snes/mario.sfc"'
        )
        # The pin landed on the Rom aggregate.
        assert uow.roms.get(42).emulator_override == "bsnes"

    def test_uninstalled_pins_without_live_launch(self, event_loop, service, uow):
        # Bound but NOT installed → pin stored, but no shortcut to update live.
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=99)
        result = event_loop.run_until_complete(service.set_game_core(42, "bsnes"))
        assert result["success"] is True
        assert result["launch_options"] is None
        assert result["app_id"] is None
        assert uow.roms.get(42).emulator_override == "bsnes"

    def test_unbound_pins_without_live_launch(self, event_loop, service, uow):
        # Installed but UNBOUND (no shortcut_app_id) → pin stored, no app_id.
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=None)
        _seed_install(uow, rom_id=42, file_path="/roms/snes/mario.sfc")
        result = event_loop.run_until_complete(service.set_game_core(42, "bsnes"))
        assert result["success"] is True
        assert result["launch_options"] is None
        assert result["app_id"] is None
        assert uow.roms.get(42).emulator_override == "bsnes"

    def test_unresolvable_label_fails_and_writes_nothing(self, event_loop, service, uow):
        # B4 + #10: an unavailable core hard-fails BEFORE any write — the DB
        # must never hold a label no consumer can resolve.
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=99)
        _seed_install(uow, rom_id=42, file_path="/roms/snes/mario.sfc")
        result = event_loop.run_until_complete(service.set_game_core(42, "Genesis Plus GX"))
        assert result["success"] is False
        assert result["reason"] == "core_unavailable"
        assert result["message"] == "Core 'Genesis Plus GX' is not available for snes"
        # No pin written.
        assert uow.roms.get(42).emulator_override is None

    def test_unknown_rom_fails(self, event_loop, service):
        result = event_loop.run_until_complete(service.set_game_core(7, "bsnes"))
        assert result["success"] is False
        assert result["reason"] == "not_found"
        assert "7" in result["message"]

    def test_resolves_system_before_label_lookup(self, event_loop, service, uow, core_info, resolve_system):
        # The slug→system normalization runs before the available-cores read so
        # label resolution keys off the RetroDECK system, not the raw slug.
        _seed_rom(uow, rom_id=42, platform_slug="dc", shortcut_app_id=99)
        _seed_install(uow, rom_id=42, file_path="/roms/dc/sonic.gdi", platform_slug="dc")
        core_info.available_cores = [{"core_so": "flycast_libretro", "label": "Flycast", "is_default": True}]
        result = event_loop.run_until_complete(service.set_game_core(42, "Flycast"))
        assert result["success"] is True
        assert ("dc", None) in resolve_system.calls
        assert core_info.available_cores_calls == ["dreamcast"]


# ── clear_game_core (Reset / Follow default) ───────────────────────────


class TestClearGameCore:
    def test_clears_and_returns_plain_launch(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=99, emulator_override="bsnes")
        _seed_install(uow, rom_id=42, file_path="/roms/snes/mario.sfc")
        result = event_loop.run_until_complete(service.clear_game_core(42))
        assert result["success"] is True
        assert result["app_id"] == 99
        # PLAIN launch — no -e override segment.
        assert result["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/snes/mario.sfc"'
        assert "-e" not in result["launch_options"]
        # The pin is gone (SQL NULL).
        assert uow.roms.get(42).emulator_override is None

    def test_clear_uninstalled_drops_pin_without_live_launch(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=99, emulator_override="bsnes")
        result = event_loop.run_until_complete(service.clear_game_core(42))
        assert result["success"] is True
        assert result["launch_options"] is None
        assert result["app_id"] is None
        assert uow.roms.get(42).emulator_override is None

    def test_clear_already_unpinned_is_idempotent(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=99)
        _seed_install(uow, rom_id=42, file_path="/roms/snes/mario.sfc")
        result = event_loop.run_until_complete(service.clear_game_core(42))
        assert result["success"] is True
        assert result["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/snes/mario.sfc"'
        assert uow.roms.get(42).emulator_override is None

    def test_clear_unknown_rom_fails(self, event_loop, service):
        result = event_loop.run_until_complete(service.clear_game_core(7))
        assert result["success"] is False
        assert result["reason"] == "not_found"


# ── set_system_core (unchanged platform-wide ES-DE override) ────────────


class TestSetSystemCore:
    def test_bios_status_carries_no_core_fields(self, event_loop, service, bios_checker):
        bios_checker.payload = {
            "needs_bios": True,
            "server_count": 1,
            "local_count": 0,
            "all_downloaded": False,
            "required_count": 1,
            "required_downloaded": 0,
            "unknown_count": 0,
            "files": [],
        }
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result["success"] is True
        bios = result["bios_status"]
        assert "active_core" not in bios
        assert "active_core_label" not in bios
        assert "available_cores" not in bios

    def test_happy_path(self, event_loop, service, core_info, gamelist_editor, bios_checker):
        bios_checker.payload = {"needs_bios": True, "files": []}
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result == {"success": True, "bios_status": {"needs_bios": True, "files": []}}
        assert gamelist_editor.system_calls == [("/home/deck/retrodeck", "snes", "Snes9x")]
        assert core_info.reset_cache_count == 1
        assert bios_checker.calls == [("snes", None)]

    def test_empty_core_label_clears_override(self, event_loop, service, gamelist_editor):
        result = event_loop.run_until_complete(service.set_system_core("snes", ""))
        assert result["success"] is True
        assert gamelist_editor.system_calls == [("/home/deck/retrodeck", "snes", None)]

    def test_no_retrodeck_home(
        self,
        event_loop,
        service,
        retrodeck_paths,
        gamelist_editor,
        bios_checker,
        core_info,
    ):
        retrodeck_paths.home = ""
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result == {"success": False, "message": "RetroDECK home not found"}
        assert gamelist_editor.system_calls == []
        assert bios_checker.calls == []
        assert core_info.reset_cache_count == 0

    def test_editor_raises_returns_error(self, event_loop, service, gamelist_editor, bios_checker):
        gamelist_editor.system_side_effect = RuntimeError("xml write failed")
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result["success"] is False
        assert "xml write failed" in result["message"]
        assert bios_checker.calls == []

    def test_bios_checker_raises_returns_error(self, event_loop, service, gamelist_editor, bios_checker):
        bios_checker.side_effect = RuntimeError("bios probe failed")
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result["success"] is False
        assert "bios probe failed" in result["message"]
        assert gamelist_editor.system_calls == [("/home/deck/retrodeck", "snes", "Snes9x")]

    @pytest.mark.parametrize(
        ("slug", "system"),
        [
            ("dc", "dreamcast"),
            ("sms", "mastersystem"),
            ("neo-geo-pocket", "ngp"),
            ("snes", "snes"),
        ],
    )
    def test_set_system_core_resolves_system_keeps_raw_bios(
        self, event_loop, service, gamelist_editor, bios_checker, slug, system
    ):
        event_loop.run_until_complete(service.set_system_core(slug, "Core"))
        assert gamelist_editor.system_calls == [("/home/deck/retrodeck", system, "Core")]
        assert bios_checker.calls == [(slug, None)]
