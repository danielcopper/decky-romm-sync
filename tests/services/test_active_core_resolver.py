"""Tests for ActiveCoreResolver — the single per-ROM active-core read seam.

Covers the four-layer precedence: a resolvable per-game override wins; a
per-platform ``settings.json`` core beats the es_systems default; the per-game
override beats the per-platform core; a NULL override with no per-platform core
delegates to the es_systems default; a stale per-game or per-platform label
degrades to the next layer without raising or emitting a bogus ``.so``; and the
retired ES-DE gamelist is never consulted.
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


class FakePlatformCoreReader:
    """In-memory ``PlatformCoreReader`` mapping platform slugs to core labels.

    Returns the configured label for a slug, or ``None`` when absent. Records
    each queried slug so a test can assert the per-platform layer was consulted
    (or skipped when a per-game override already resolved).
    """

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {}
        self.calls: list[str] = []

    def get_platform_core(self, platform_slug: str) -> str | None:
        self.calls.append(platform_slug)
        return self.mapping.get(platform_slug)


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
    platform_core_reader: FakePlatformCoreReader | None = None,
) -> tuple[ActiveCoreResolver, FakeSystemResolver]:
    resolver_fn = resolve_system if resolve_system is not None else FakeSystemResolver()
    platform_reader = platform_core_reader if platform_core_reader is not None else FakePlatformCoreReader()
    resolver = ActiveCoreResolver(
        config=ActiveCoreResolverConfig(
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
            core_info=core_info,
            platform_core_reader=platform_reader,
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


# --- per-platform layer beats es_systems default; per-game beats per-platform ---


def test_per_platform_core_beats_es_systems_default() -> None:
    """An un-pinned ROM whose platform carries a per-platform core gets that core,
    not the es_systems default — the layer-2 selection wins over the system layer."""
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=20, platform_slug="snes", emulator_override=None)
    core_info = FakeCoreInfoProvider(
        available_cores=[
            {"core_so": "snes9x_libretro", "label": "Snes9x", "is_default": True},
            {"core_so": "bsnes_libretro", "label": "bsnes", "is_default": False},
        ],
        # es_systems default is Snes9x — the per-platform core must override it.
        active_core=("snes9x_libretro", "Snes9x"),
    )
    platform_reader = FakePlatformCoreReader(mapping={"snes": "bsnes"})
    resolver, _ = _make_resolver(uow=uow, core_info=core_info, platform_core_reader=platform_reader)

    assert resolver.active_core_for_rom(20) == ("bsnes_libretro", "bsnes")
    # The es_systems default layer was never consulted — the per-platform core resolved.
    assert core_info.active_core_calls == []
    assert platform_reader.calls == ["snes"]


def test_per_game_override_beats_per_platform_core() -> None:
    """A pinned ROM keeps its per-game core even when its platform has a per-platform
    selection — layer-1 (per-game) wins over layer-2 (per-platform)."""
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=21, platform_slug="snes", emulator_override="Snes9x")
    core_info = FakeCoreInfoProvider(
        available_cores=[
            {"core_so": "snes9x_libretro", "label": "Snes9x", "is_default": True},
            {"core_so": "bsnes_libretro", "label": "bsnes", "is_default": False},
        ],
        active_core=("bsnes_libretro", "bsnes"),
    )
    platform_reader = FakePlatformCoreReader(mapping={"snes": "bsnes"})
    resolver, _ = _make_resolver(uow=uow, core_info=core_info, platform_core_reader=platform_reader)

    assert resolver.active_core_for_rom(21) == ("snes9x_libretro", "Snes9x")
    # Per-game override resolved first — the per-platform layer is never consulted.
    assert platform_reader.calls == []
    assert core_info.active_core_calls == []


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


# --- stale per-platform core → degrades to es_systems default (no raise) -------


def test_stale_per_platform_core_degrades_to_system_default() -> None:
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=30, platform_slug="gba", emulator_override=None)
    # available_cores no longer carries the per-platform label → degrades.
    core_info = FakeCoreInfoProvider(
        available_cores=[{"core_so": "mgba_libretro", "label": "mGBA", "is_default": True}],
        active_core=("mgba_libretro", "mGBA"),
    )
    platform_reader = FakePlatformCoreReader(mapping={"gba": "Removed Core"})
    resolver, _ = _make_resolver(uow=uow, core_info=core_info, platform_core_reader=platform_reader)

    result = resolver.active_core_for_rom(30)

    # Degrades to the es_systems default — never a bogus "None.so", never raises.
    assert result == ("mgba_libretro", "mGBA")
    assert platform_reader.calls == ["gba"]
    assert core_info.active_core_calls == ["gba"]


def test_stale_per_platform_core_logs_warning(caplog: pytest.LogCaptureFixture) -> None:
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=30, platform_slug="gba", emulator_override=None)
    core_info = FakeCoreInfoProvider(
        available_cores=[{"core_so": "mgba_libretro", "label": "mGBA", "is_default": True}],
        active_core=("mgba_libretro", "mGBA"),
    )
    platform_reader = FakePlatformCoreReader(mapping={"gba": "Removed Core"})
    resolver, _ = _make_resolver(uow=uow, core_info=core_info, platform_core_reader=platform_reader)

    with caplog.at_level(logging.WARNING, logger="test"):
        resolver.active_core_for_rom(30)

    assert any("Removed Core" in r.message and "per-platform" in r.message for r in caplog.records)


# --- no per-platform core → falls through to es_systems default ----------------


def test_no_per_platform_core_falls_through_to_system_default() -> None:
    """A NULL override + an empty per-platform map delegates straight to the
    es_systems default — the per-platform layer was consulted and found nothing."""
    uow = FakeUnitOfWork()
    _seed_rom(uow, rom_id=40, platform_slug="snes", emulator_override=None)
    core_info = FakeCoreInfoProvider(
        available_cores=[{"core_so": "snes9x_libretro", "label": "Snes9x", "is_default": True}],
        active_core=("snes9x_libretro", "Snes9x"),
    )
    platform_reader = FakePlatformCoreReader()  # empty map → None for every slug
    resolver, _ = _make_resolver(uow=uow, core_info=core_info, platform_core_reader=platform_reader)

    assert resolver.active_core_for_rom(40) == ("snes9x_libretro", "Snes9x")
    assert platform_reader.calls == ["snes"]
    assert core_info.active_core_calls == ["snes"]


# --- bad path: unknown rom_id → (None, None), no raise -------------------------


def test_missing_rom_resolves_to_none_with_warning(caplog: pytest.LogCaptureFixture) -> None:
    uow = FakeUnitOfWork()
    core_info = FakeCoreInfoProvider(active_core=("mgba_libretro", "mGBA"))
    platform_reader = FakePlatformCoreReader(mapping={"gba": "mGBA"})
    resolver, _ = _make_resolver(uow=uow, core_info=core_info, platform_core_reader=platform_reader)

    with caplog.at_level(logging.WARNING, logger="test"):
        result = resolver.active_core_for_rom(404)

    assert result == (None, None)
    # No system read happens for a ROM that does not exist — and the per-platform
    # layer (the retired-gamelist replacement) is never consulted for a missing ROM.
    assert core_info.active_core_calls == []
    assert platform_reader.calls == []
    assert any("404" in r.message for r in caplog.records)
