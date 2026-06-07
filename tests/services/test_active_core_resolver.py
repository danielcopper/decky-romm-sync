"""Tests for ActiveCoreResolver — the single per-ROM active-core read seam.

Covers the four contract branches: a resolvable override wins; a NULL override
delegates to the system layer; the system ``<alternativeEmulator>`` layer is
preserved through that delegation (combined precedence); and a stale override
degrades to the system default without raising or emitting a bogus ``.so``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.rom import Rom
from services.active_core_resolver import ActiveCoreResolver, ActiveCoreResolverConfig

if TYPE_CHECKING:
    import pytest


class FakeSystemResolver:
    """In-memory ``SystemResolver`` mapping platform slugs to RetroDECK systems.

    Records each call so a test can assert the resolver normalized the ROM's
    platform slug before reaching the core read seams. Unknown slugs pass
    through unchanged, mirroring the real resolver.
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {}
        self.calls: list[tuple[str, str | None]] = []

    def __call__(self, platform_slug: str, platform_fs_slug: str | None = None) -> str:
        self.calls.append((platform_slug, platform_fs_slug))
        return self.mapping.get(platform_slug, platform_slug)


def _seed_rom(
    uow: FakeUnitOfWork,
    *,
    rom_id: int,
    platform_slug: str,
    emulator_override: str | None = None,
) -> None:
    """Seed one ``Rom`` (optionally pinned to ``emulator_override``) into the UoW."""
    uow.roms.save(
        Rom(
            rom_id=rom_id,
            platform_slug=platform_slug,
            name=f"rom-{rom_id}",
            fs_name=f"rom-{rom_id}.gba",
            shortcut_app_id=rom_id,
            last_synced_at="2026-01-01T00:00:00+00:00",
            emulator_override=emulator_override,
        )
    )


def _make_resolver(
    *,
    uow: FakeUnitOfWork,
    core_info: FakeCoreInfoProvider,
    resolve_system: FakeSystemResolver | None = None,
) -> tuple[ActiveCoreResolver, FakeSystemResolver]:
    resolver_fn = resolve_system if resolve_system is not None else FakeSystemResolver()
    resolver = ActiveCoreResolver(
        config=ActiveCoreResolverConfig(
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
            core_info=core_info,
            resolve_system=resolver_fn,
            logger=logging.getLogger("test"),
        ),
    )
    return resolver, resolver_fn


# --- override set + resolvable → returns the override's (core_so, label) -------


def test_resolvable_override_returns_pinned_core() -> None:
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=42, platform_slug="gba", emulator_override="mGBA")
    core_info = FakeCoreInfoProvider(
        available_cores=[
            {"core_so": "vba_next_libretro", "label": "VBA Next", "is_default": True},
            {"core_so": "mgba_libretro", "label": "mGBA", "is_default": False},
        ],
        # System default differs from the pin so the test proves the override won.
        active_core=("vba_next_libretro", "VBA Next"),
    )
    resolver, _ = _make_resolver(uow=uow, core_info=core_info)

    assert resolver.active_core_for_rom(42) == ("mgba_libretro", "mGBA")
    # System-layer get_active_core must NOT be consulted when the override resolves.
    assert core_info.active_core_calls == []


def test_resolvable_override_normalizes_platform_slug_to_system() -> None:
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=7, platform_slug="gba", emulator_override="mGBA")
    core_info = FakeCoreInfoProvider(
        available_cores=[{"core_so": "mgba_libretro", "label": "mGBA", "is_default": True}],
    )
    resolver, resolve_system = _make_resolver(
        uow=uow,
        core_info=core_info,
        resolve_system=FakeSystemResolver(mapping={"gba": "gba"}),
    )

    resolver.active_core_for_rom(7)
    # The available-cores read seam must receive the resolved system, not the raw slug.
    assert resolve_system.calls == [("gba", None)]
    assert core_info.available_cores_calls == ["gba"]


# --- override NULL → returns the system-default (delegation works) -------------


def test_null_override_delegates_to_system_default() -> None:
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=1, platform_slug="snes", emulator_override=None)
    core_info = FakeCoreInfoProvider(
        available_cores=[{"core_so": "snes9x_libretro", "label": "Snes9x", "is_default": True}],
        active_core=("snes9x_libretro", "Snes9x"),
    )
    resolver, _ = _make_resolver(uow=uow, core_info=core_info)

    assert resolver.active_core_for_rom(1) == ("snes9x_libretro", "Snes9x")
    # Delegation path: the system-layer get_active_core was consulted with the system only.
    assert core_info.active_core_calls == ["snes"]


def test_null_override_passes_through_system_none() -> None:
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=5, platform_slug="unknown", emulator_override=None)
    core_info = FakeCoreInfoProvider(active_core=(None, None))
    resolver, _ = _make_resolver(uow=uow, core_info=core_info)

    # An unconfigured system yields (None, None); that passes through unchanged.
    assert resolver.active_core_for_rom(5) == (None, None)


# --- combined precedence: system alt-emu preserved through delegation (R7) -----


def test_null_override_returns_system_alt_emulator_not_platform_default() -> None:
    """One platform, two ROMs: pinned ROM gets its pin; the un-pinned ROM gets the
    system ``<alternativeEmulator>`` (NOT the es_systems default), proving step-3
    delegation preserves the whole system layer (review R7)."""
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=10, platform_slug="psx", emulator_override="PCSX ReARMed")
    _seed_rom(uow, rom_id=11, platform_slug="psx", emulator_override=None)
    # get_active_core models the live system layer: a system alt-emulator override
    # is set on this platform, so it returns the alt-emu core, not the es_systems
    # default. The resolver must surface exactly that for the un-pinned ROM.
    core_info = FakeCoreInfoProvider(
        available_cores=[
            {"core_so": "swanstation_libretro", "label": "SwanStation", "is_default": True},
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": False},
            {"core_so": "beetle_psx_libretro", "label": "Beetle PSX", "is_default": False},
        ],
        active_core=("beetle_psx_libretro", "Beetle PSX"),  # the system alt-emu, not the default
    )
    resolver, _ = _make_resolver(uow=uow, core_info=core_info)

    # Pinned ROM → its own override (system layer untouched).
    assert resolver.active_core_for_rom(10) == ("pcsx_rearmed_libretro", "PCSX ReARMed")
    # Un-pinned ROM → the system alt-emu core, not the es_systems default.
    assert resolver.active_core_for_rom(11) == ("beetle_psx_libretro", "Beetle PSX")


# --- override set but STALE → degrades to system default (no raise, no bogus so) ---


def test_stale_override_degrades_to_system_default() -> None:
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=99, platform_slug="gba", emulator_override="Removed Core")
    # available_cores no longer carries "Removed Core" → label_to_core_so → None.
    core_info = FakeCoreInfoProvider(
        available_cores=[{"core_so": "mgba_libretro", "label": "mGBA", "is_default": True}],
        active_core=("mgba_libretro", "mGBA"),
    )
    resolver, _ = _make_resolver(uow=uow, core_info=core_info)

    result = resolver.active_core_for_rom(99)

    # Degrades to the system default — never a bogus "None.so", never raises.
    assert result == ("mgba_libretro", "mGBA")
    # The system layer was consulted (the degrade delegated past the unresolvable pin).
    assert core_info.active_core_calls == ["gba"]


def test_stale_override_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=99, platform_slug="gba", emulator_override="Removed Core")
    core_info = FakeCoreInfoProvider(
        available_cores=[{"core_so": "mgba_libretro", "label": "mGBA", "is_default": True}],
        active_core=("mgba_libretro", "mGBA"),
    )
    resolver, _ = _make_resolver(uow=uow, core_info=core_info)

    with caplog.at_level(logging.WARNING, logger="test"):
        resolver.active_core_for_rom(99)

    assert any("Removed Core" in r.message and "degrading" in r.message for r in caplog.records)


# --- bad path: unknown rom_id → (None, None), no raise -------------------------


def test_missing_rom_resolves_to_none_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    uow = FakeUnitOfWork()
    core_info = FakeCoreInfoProvider(active_core=("mgba_libretro", "mGBA"))
    resolver, _ = _make_resolver(uow=uow, core_info=core_info)

    with caplog.at_level(logging.WARNING, logger="test"):
        result = resolver.active_core_for_rom(404)

    assert result == (None, None)
    # No system read happens for a ROM that does not exist.
    assert core_info.active_core_calls == []
    assert any("404" in r.message for r in caplog.records)
