"""Tests for CoreService — per-platform core write + fan-out + per-game pin/clear + core menu."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_settings_persister import FakeSettingsPersister
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.rom import Rom
from domain.rom_install import RomInstall
from services.cores import CoreService, CoreServiceConfig


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
def settings() -> dict[str, Any]:
    return {"platform_cores": {}}


@pytest.fixture
def settings_persister() -> FakeSettingsPersister:
    return FakeSettingsPersister()


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
    resolve_system,
    settings,
    settings_persister,
    bios_checker,
    uow_factory,
    active_core,
) -> CoreService:
    return CoreService(
        config=CoreServiceConfig(
            loop=event_loop,
            logger=logger,
            core_info=core_info,
            resolve_system=resolve_system,
            settings=settings,
            settings_persister=settings_persister,
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
            "platform_core_label": None,
            "has_game_override": False,
        }

    def test_unknown_rom_returns_empty(self, event_loop, service, core_info):
        # No ROM seeded for rom_id=7 → empty cores + no active core, and the
        # platform-wide core enumeration is never reached.
        result = event_loop.run_until_complete(service.get_available_cores(7))
        assert result == {
            "cores": [],
            "active_core": None,
            "active_core_label": None,
            "platform_core_label": None,
            "has_game_override": False,
        }
        assert core_info.available_cores_calls == []

    def test_platform_core_label_surfaces_per_platform_override(self, event_loop, service, uow, settings):
        # A per-platform override set on the System page (settings.json
        # platform_cores) surfaces as platform_core_label so the menu can mark
        # the system-level selection distinctly from the active core (#954).
        _seed_rom(uow, rom_id=42, platform_slug="snes")
        settings["platform_cores"]["snes"] = "bsnes"
        result = event_loop.run_until_complete(service.get_available_cores(42))
        assert result["platform_core_label"] == "bsnes"

    def test_platform_core_label_none_when_platform_absent(self, event_loop, service, uow, settings):
        # A platform with no per-platform override → platform_core_label is None
        # even when OTHER platforms carry one.
        _seed_rom(uow, rom_id=42, platform_slug="snes")
        settings["platform_cores"]["gba"] = "mGBA"
        result = event_loop.run_until_complete(service.get_available_cores(42))
        assert result["platform_core_label"] is None

    def test_active_marker_reflects_pin(self, event_loop, service, uow, active_core):
        # A pinned ROM surfaces the OVERRIDE core as active (via the resolver),
        # not the system default — the menu highlights the pin.
        _seed_rom(uow, rom_id=42, platform_slug="snes", emulator_override="bsnes")
        active_core.per_rom[42] = ("bsnes_libretro", "bsnes")
        result = event_loop.run_until_complete(service.get_available_cores(42))
        assert result["active_core"] == "bsnes_libretro"
        assert result["active_core_label"] == "bsnes"
        assert active_core.calls == [42]

    def test_has_game_override_true_when_rom_is_pinned(self, event_loop, service, uow):
        # A ROM with a per-game emulator_override surfaces has_game_override=True
        # so the menu's "Use System Override" reset item drops its ✓ (#211). The
        # frontend can't infer this from the active core — pinning the same core
        # as the per-platform override would be indistinguishable.
        _seed_rom(uow, rom_id=42, platform_slug="snes", emulator_override="bsnes")
        result = event_loop.run_until_complete(service.get_available_cores(42))
        assert result["has_game_override"] is True

    def test_has_game_override_false_when_rom_unpinned(self, event_loop, service, uow):
        # An unpinned ROM (NULL override) follows the system → has_game_override
        # is False so the reset item carries the ✓ (#211).
        _seed_rom(uow, rom_id=42, platform_slug="snes")
        result = event_loop.run_until_complete(service.get_available_cores(42))
        assert result["has_game_override"] is False

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
    def test_clears_and_bakes_resolved_active_core(self, event_loop, service, uow, active_core):
        # After clearing the pin, the ROM follows the per-platform/system default.
        # The resolver yields that default (here a per-platform bsnes); the bake
        # must carry the -e override for the resolved core, NOT a plain launch.
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=99, emulator_override="Snes9x")
        _seed_install(uow, rom_id=42, file_path="/roms/snes/mario.sfc")
        active_core.per_rom[42] = ("bsnes_libretro", "bsnes")
        result = event_loop.run_until_complete(service.clear_game_core(42))
        assert result["success"] is True
        assert result["app_id"] == 99
        # The resolver was consulted AFTER the pin cleared, and the bake uses its
        # resolved core (the per-platform default) with the -e override form.
        assert active_core.calls == [42]
        assert result["launch_options"] == (
            "flatpak run net.retrodeck.retrodeck -e "
            '"%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/bsnes_libretro.so %ROM%" '
            '"/roms/snes/mario.sfc"'
        )
        # The pin is gone (SQL NULL).
        assert uow.roms.get(42).emulator_override is None

    def test_clears_and_bakes_plain_when_platform_unresolvable(self, event_loop, service, uow, active_core):
        # When the resolver yields (None, None) — a genuinely unresolvable
        # platform — clearing bakes the plain launch (no -e).
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=99, emulator_override="bsnes")
        _seed_install(uow, rom_id=42, file_path="/roms/snes/mario.sfc")
        active_core.per_rom[42] = (None, None)
        result = event_loop.run_until_complete(service.clear_game_core(42))
        assert result["success"] is True
        assert result["launch_options"] == 'flatpak run net.retrodeck.retrodeck "/roms/snes/mario.sfc"'
        assert "-e" not in result["launch_options"]
        assert uow.roms.get(42).emulator_override is None

    def test_clear_uninstalled_drops_pin_without_live_launch(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=42, platform_slug="snes", shortcut_app_id=99, emulator_override="bsnes")
        result = event_loop.run_until_complete(service.clear_game_core(42))
        assert result["success"] is True
        assert result["launch_options"] is None
        assert result["app_id"] is None
        assert uow.roms.get(42).emulator_override is None

    def test_clear_unknown_rom_fails(self, event_loop, service):
        result = event_loop.run_until_complete(service.clear_game_core(7))
        assert result["success"] is False
        assert result["reason"] == "not_found"


# ── set_system_core (per-platform settings write) ──────────────────────


class TestSetSystemCore:
    def test_writes_label_to_settings_and_persists(self, event_loop, service, settings, settings_persister):
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result["success"] is True
        assert settings["platform_cores"] == {"snes": "Snes9x"}
        assert settings_persister.save_count == 1

    def test_empty_core_label_clears_settings_entry(self, event_loop, service, settings, settings_persister):
        settings["platform_cores"]["snes"] = "Snes9x"
        result = event_loop.run_until_complete(service.set_system_core("snes", ""))
        assert result["success"] is True
        # Clearing removes the platform from the map (revert to es_systems default).
        assert "snes" not in settings["platform_cores"]
        assert settings_persister.save_count == 1

    def test_clearing_absent_platform_is_noop(self, event_loop, service, settings):
        # Clearing a platform with no prior selection just leaves the map empty.
        result = event_loop.run_until_complete(service.set_system_core("psx", ""))
        assert result["success"] is True
        assert settings["platform_cores"] == {}

    def test_rechecks_bios_and_invalidates_core_cache(self, event_loop, service, core_info, bios_checker):
        bios_checker.payload = {"needs_bios": True, "files": []}
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result["bios_status"] == {"needs_bios": True, "files": []}
        assert core_info.reset_cache_count == 1
        # The platform-level recheck passes None (no per-game core).
        assert bios_checker.calls == [("snes", None)]

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

    def test_bios_checker_raises_returns_error(self, event_loop, service, settings, bios_checker):
        bios_checker.side_effect = RuntimeError("bios probe failed")
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result["success"] is False
        assert "bios probe failed" in result["message"]
        # The settings write already landed before the BIOS recheck raised.
        assert settings["platform_cores"] == {"snes": "Snes9x"}


# ── set_system_core fan-out (re-bake installed+bound ROMs on the platform) ──


class TestSetSystemCoreFanOut:
    def test_includes_installed_and_bound_with_override_launch(self, event_loop, service, uow, active_core):
        # Two installed+bound ROMs on snes; the new per-platform core resolves to
        # bsnes for both → both appear in rebake_items with the -e override.
        _seed_rom(uow, rom_id=1, platform_slug="snes", shortcut_app_id=101)
        _seed_install(uow, rom_id=1, file_path="/roms/snes/a.sfc")
        _seed_rom(uow, rom_id=2, platform_slug="snes", shortcut_app_id=102)
        _seed_install(uow, rom_id=2, file_path="/roms/snes/b.sfc")
        active_core.per_rom[1] = ("bsnes_libretro", "bsnes")
        active_core.per_rom[2] = ("bsnes_libretro", "bsnes")
        result = event_loop.run_until_complete(service.set_system_core("snes", "bsnes"))
        items = {item["app_id"]: item["launch_options"] for item in result["rebake_items"]}
        assert items == {
            101: (
                "flatpak run net.retrodeck.retrodeck -e "
                '"%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/bsnes_libretro.so %ROM%" '
                '"/roms/snes/a.sfc"'
            ),
            102: (
                "flatpak run net.retrodeck.retrodeck -e "
                '"%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/bsnes_libretro.so %ROM%" '
                '"/roms/snes/b.sfc"'
            ),
        }

    def test_skips_per_game_overridden_rom(self, event_loop, service, uow, active_core):
        # A ROM with its own emulator_override is NOT re-baked — the per-game pin
        # wins over the platform default, so its shortcut must not be touched.
        _seed_rom(uow, rom_id=1, platform_slug="snes", shortcut_app_id=101)
        _seed_install(uow, rom_id=1, file_path="/roms/snes/a.sfc")
        _seed_rom(uow, rom_id=2, platform_slug="snes", shortcut_app_id=102, emulator_override="Snes9x")
        _seed_install(uow, rom_id=2, file_path="/roms/snes/b.sfc")
        active_core.per_rom[1] = ("bsnes_libretro", "bsnes")
        result = event_loop.run_until_complete(service.set_system_core("snes", "bsnes"))
        app_ids = [item["app_id"] for item in result["rebake_items"]]
        assert app_ids == [101]
        # The overridden ROM's resolver is never consulted.
        assert active_core.calls == [1]

    def test_skips_uninstalled_rom(self, event_loop, service, uow, active_core):
        # Bound but NOT installed → no live launch command to rewrite.
        _seed_rom(uow, rom_id=1, platform_slug="snes", shortcut_app_id=101)
        _seed_install(uow, rom_id=1, file_path="/roms/snes/a.sfc")
        _seed_rom(uow, rom_id=2, platform_slug="snes", shortcut_app_id=102)  # no install
        active_core.per_rom[1] = ("bsnes_libretro", "bsnes")
        result = event_loop.run_until_complete(service.set_system_core("snes", "bsnes"))
        app_ids = [item["app_id"] for item in result["rebake_items"]]
        assert app_ids == [101]

    def test_skips_unbound_rom(self, event_loop, service, uow, active_core):
        # Installed but UNBOUND (no shortcut_app_id) → no shortcut to rewrite.
        _seed_rom(uow, rom_id=1, platform_slug="snes", shortcut_app_id=101)
        _seed_install(uow, rom_id=1, file_path="/roms/snes/a.sfc")
        _seed_rom(uow, rom_id=2, platform_slug="snes", shortcut_app_id=None)
        _seed_install(uow, rom_id=2, file_path="/roms/snes/b.sfc")
        active_core.per_rom[1] = ("bsnes_libretro", "bsnes")
        result = event_loop.run_until_complete(service.set_system_core("snes", "bsnes"))
        app_ids = [item["app_id"] for item in result["rebake_items"]]
        assert app_ids == [101]

    def test_bakes_plain_launch_when_resolver_yields_none(self, event_loop, service, uow, active_core):
        # Clearing the per-platform core: the resolver yields (None, None) for an
        # unresolvable platform → the re-bake carries the plain launch (no -e).
        _seed_rom(uow, rom_id=1, platform_slug="snes", shortcut_app_id=101)
        _seed_install(uow, rom_id=1, file_path="/roms/snes/a.sfc")
        active_core.per_rom[1] = (None, None)
        result = event_loop.run_until_complete(service.set_system_core("snes", ""))
        items = result["rebake_items"]
        assert items == [
            {
                "app_id": 101,
                "launch_options": 'flatpak run net.retrodeck.retrodeck "/roms/snes/a.sfc"',
            }
        ]

    def test_only_platform_roms_are_rebaked(self, event_loop, service, uow, active_core):
        # A ROM on a different platform must never appear in the fan-out.
        _seed_rom(uow, rom_id=1, platform_slug="snes", shortcut_app_id=101)
        _seed_install(uow, rom_id=1, file_path="/roms/snes/a.sfc")
        _seed_rom(uow, rom_id=2, platform_slug="gba", shortcut_app_id=102)
        _seed_install(uow, rom_id=2, file_path="/roms/gba/b.gba", platform_slug="gba")
        active_core.per_rom[1] = ("bsnes_libretro", "bsnes")
        result = event_loop.run_until_complete(service.set_system_core("snes", "bsnes"))
        app_ids = [item["app_id"] for item in result["rebake_items"]]
        assert app_ids == [101]

    def test_empty_fan_out_when_no_matching_roms(self, event_loop, service):
        # No ROMs on the platform → empty rebake_items, success still True.
        result = event_loop.run_until_complete(service.set_system_core("snes", "bsnes"))
        assert result["success"] is True
        assert result["rebake_items"] == []
