"""Tests for RelaunchOptionsResolver — the shared installed+bound relaunch seam.

The single (deadlock-free) build of the ``{app_id, launch_options}`` items the
RetroDECK-home migration, the startup launch-options reconcile, and the
Play-button pre-launch re-confirm (#1150) all delegate to. These cases pin the
resolution behavior — empty, skip-on-missing-rom, skip-unbound, default core,
``-e`` core-override form, multiple installs, and the multi-disc pin — over the
fake active-core / disc seams and the fake UoW, for both the batch
(``installed_relaunch_items``) and single-ROM (``relaunch_item_for_rom``)
entry points that share the one resolve body.
"""

from __future__ import annotations

from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.disc_selection import Disc
from domain.rom import Rom
from domain.rom_install import RomInstall
from services.relaunch_options_resolver import RelaunchOptionsResolver, RelaunchOptionsResolverConfig


def _make_rom(rom_id: int, *, shortcut_app_id: int | None) -> Rom:
    return Rom(
        rom_id=rom_id,
        platform_slug="n64",
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.z64",
        shortcut_app_id=shortcut_app_id,
        last_synced_at="2025-01-01T00:00:00",
    )


def _seed_install(
    uow: FakeUnitOfWork,
    rom_id: int,
    *,
    file_path: str,
    rom_dir: str | None = None,
    shortcut_app_id: int | None,
    selected_disc: str | None = None,
) -> None:
    """Seed the FK-parent Rom THEN its install record, in one commit."""
    install = RomInstall.mark_installed(
        rom_id=rom_id,
        file_path=file_path,
        rom_dir=rom_dir,
        platform_slug="n64",
        system="n64",
        installed_at="2025-01-01T00:00:00",
    )
    with uow:
        uow.roms.save(_make_rom(rom_id, shortcut_app_id=shortcut_app_id))
        uow.rom_installs.save(install)
        if selected_disc is not None:
            uow.roms.set_selected_disc(rom_id, selected_disc)


def _make_resolver(
    *,
    uow: FakeUnitOfWork,
    active_core: FakeActiveCoreResolver | None = None,
    disc_resolver: FakeDiscResolver | None = None,
) -> RelaunchOptionsResolver:
    return RelaunchOptionsResolver(
        config=RelaunchOptionsResolverConfig(
            uow_factory=FakeUnitOfWorkFactory(uow=uow),
            active_core=active_core if active_core is not None else FakeActiveCoreResolver(),
            disc_resolver=disc_resolver if disc_resolver is not None else FakeDiscResolver(),
        ),
    )


def test_no_installs_returns_empty():
    """No rom_installs rows → empty list (nothing to relaunch)."""
    resolver = _make_resolver(uow=FakeUnitOfWork())
    assert resolver.installed_relaunch_items() == []


def test_skips_install_when_rom_lookup_returns_none(monkeypatch):
    """Defensive skip: an install whose ``roms.get`` yields None is dropped.

    The real schema's FK keeps this from happening on disk, so the branch is
    forced by stubbing the lookup rather than by orphaning the install (the
    FK-modelling fake would reject that at commit).
    """
    uow = FakeUnitOfWork()
    _seed_install(uow, 1, file_path="/roms/n64/a.z64", shortcut_app_id=99)
    monkeypatch.setattr(uow.roms, "get", lambda _rom_id: None)
    resolver = _make_resolver(uow=uow)
    assert resolver.installed_relaunch_items() == []


def test_skips_unbound_rom():
    """An installed ROM with shortcut_app_id=None (unbound) is skipped."""
    uow = FakeUnitOfWork()
    _seed_install(uow, 1, file_path="/roms/n64/a.z64", shortcut_app_id=None)
    resolver = _make_resolver(uow=uow)
    assert resolver.installed_relaunch_items() == []


def test_single_installed_bound_default_core():
    """Installed+bound, core resolves None → plain flatpak launch command."""
    uow = FakeUnitOfWork()
    file_path = "/roms/n64/zelda.z64"
    _seed_install(uow, 1, file_path=file_path, shortcut_app_id=4242)
    resolver = _make_resolver(uow=uow)
    assert resolver.installed_relaunch_items() == [
        {
            "app_id": 4242,
            "launch_options": f'flatpak run net.retrodeck.retrodeck "{file_path}"',
        }
    ]


def test_core_override_bakes_e_form():
    """A resolved core .so produces the RetroDECK -e override in the command."""
    uow = FakeUnitOfWork()
    file_path = "/roms/n64/mario.z64"
    _seed_install(uow, 1, file_path=file_path, shortcut_app_id=7)
    active_core = FakeActiveCoreResolver(per_rom={1: ("mupen64plus_next", "Mupen64Plus-Next")})
    resolver = _make_resolver(uow=uow, active_core=active_core)
    assert resolver.installed_relaunch_items() == [
        {
            "app_id": 7,
            "launch_options": (
                "flatpak run net.retrodeck.retrodeck -e "
                '"%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/mupen64plus_next.so %ROM%" '
                f'"{file_path}"'
            ),
        }
    ]
    assert active_core.calls == [1]


def test_multiple_installs_yield_multiple_items():
    """Every installed+bound ROM contributes one item, in iteration order."""
    uow = FakeUnitOfWork()
    _seed_install(uow, 1, file_path="/roms/n64/a.z64", shortcut_app_id=11)
    _seed_install(uow, 2, file_path="/roms/n64/b.z64", shortcut_app_id=22)
    resolver = _make_resolver(uow=uow)
    items = resolver.installed_relaunch_items()
    assert items == [
        {"app_id": 11, "launch_options": 'flatpak run net.retrodeck.retrodeck "/roms/n64/a.z64"'},
        {"app_id": 22, "launch_options": 'flatpak run net.retrodeck.retrodeck "/roms/n64/b.z64"'},
    ]


_ROM_DIR = "/roms/psx/game-1"
_DISC1 = "Game (Disc 1).cue"
_DISC2 = "Game (Disc 2).cue"
_DISC1_PATH = f"{_ROM_DIR}/{_DISC1}"
_DISC2_PATH = f"{_ROM_DIR}/{_DISC2}"


def _multi_disc_resolver() -> FakeDiscResolver:
    resolver = FakeDiscResolver()
    resolver.set_discs(
        _ROM_DIR,
        [
            Disc(filename=_DISC1, path=_DISC1_PATH, label="Disc 1", index=1),
            Disc(filename=_DISC2, path=_DISC2_PATH, label="Disc 2", index=2),
        ],
    )
    return resolver


def _seed_multi_disc(uow: FakeUnitOfWork, *, rom_id: int, selected_disc: str | None, app_id: int) -> None:
    with uow:
        uow.roms.save(
            Rom(
                rom_id=rom_id,
                platform_slug="psx",
                name=f"rom-{rom_id}",
                fs_name=f"rom-{rom_id}",
                shortcut_app_id=app_id,
                last_synced_at="2026-01-01T00:00:00+00:00",
            )
        )
        uow.rom_installs.save(
            RomInstall(
                rom_id=rom_id,
                file_path=_DISC1_PATH,
                rom_dir=_ROM_DIR,
                platform_slug="psx",
                system="psx",
                installed_at="2026-01-01T00:00:00+00:00",
            )
        )
        if selected_disc is not None:
            uow.roms.set_selected_disc(rom_id, selected_disc)


def test_multi_disc_pin_bakes_selected_disc_path():
    """A multi-disc ROM pinned to disc 2 bakes disc 2's path."""
    uow = FakeUnitOfWork()
    _seed_multi_disc(uow, rom_id=1, selected_disc=_DISC2, app_id=555)
    resolver = _make_resolver(uow=uow, disc_resolver=_multi_disc_resolver())
    items = resolver.installed_relaunch_items()
    assert items == [{"app_id": 555, "launch_options": f'flatpak run net.retrodeck.retrodeck "{_DISC2_PATH}"'}]


def test_multi_disc_unpinned_defaults_to_disc_1():
    """An unpinned multi-disc ROM bakes disc 1's path (the file_path default)."""
    uow = FakeUnitOfWork()
    _seed_multi_disc(uow, rom_id=1, selected_disc=None, app_id=555)
    resolver = _make_resolver(uow=uow, disc_resolver=_multi_disc_resolver())
    items = resolver.installed_relaunch_items()
    assert items[0]["launch_options"] == f'flatpak run net.retrodeck.retrodeck "{_DISC1_PATH}"'


# ── relaunch_item_for_rom — the single-ROM re-confirm seam (#1150) ──────────


def test_single_rom_installed_bound_default_core():
    """One installed+bound ROM (no core override) → its plain launch command."""
    uow = FakeUnitOfWork()
    file_path = "/roms/n64/zelda.z64"
    _seed_install(uow, 1, file_path=file_path, shortcut_app_id=4242)
    resolver = _make_resolver(uow=uow)
    assert resolver.relaunch_item_for_rom(1) == {
        "app_id": 4242,
        "launch_options": f'flatpak run net.retrodeck.retrodeck "{file_path}"',
    }


def test_single_rom_core_override_bakes_e_form():
    """A resolved core .so produces the RetroDECK -e override for the single ROM."""
    uow = FakeUnitOfWork()
    file_path = "/roms/n64/mario.z64"
    _seed_install(uow, 1, file_path=file_path, shortcut_app_id=7)
    active_core = FakeActiveCoreResolver(per_rom={1: ("mupen64plus_next", "Mupen64Plus-Next")})
    resolver = _make_resolver(uow=uow, active_core=active_core)
    assert resolver.relaunch_item_for_rom(1) == {
        "app_id": 7,
        "launch_options": (
            "flatpak run net.retrodeck.retrodeck -e "
            '"%EMULATOR_RETROARCH% -L /var/config/retroarch/cores/mupen64plus_next.so %ROM%" '
            f'"{file_path}"'
        ),
    }
    assert active_core.calls == [1]


def test_single_rom_no_install_row_returns_none():
    """A ROM with no rom_installs row (uninstalled) → None, nothing to confirm."""
    uow = FakeUnitOfWork()
    with uow:
        uow.roms.save(_make_rom(1, shortcut_app_id=99))
    resolver = _make_resolver(uow=uow)
    assert resolver.relaunch_item_for_rom(1) is None


def test_single_rom_unbound_returns_none():
    """An installed ROM with shortcut_app_id=None (unbound) → None."""
    uow = FakeUnitOfWork()
    _seed_install(uow, 1, file_path="/roms/n64/a.z64", shortcut_app_id=None)
    resolver = _make_resolver(uow=uow)
    assert resolver.relaunch_item_for_rom(1) is None


def test_single_rom_missing_rom_returns_none(monkeypatch):
    """An install whose ``roms.get`` yields None (defensive) → None.

    The schema FK keeps this from happening on disk, so the branch is forced by
    stubbing the lookup rather than orphaning the install (the FK-modelling fake
    rejects an orphan at commit).
    """
    uow = FakeUnitOfWork()
    _seed_install(uow, 1, file_path="/roms/n64/a.z64", shortcut_app_id=99)
    monkeypatch.setattr(uow.roms, "get", lambda _rom_id: None)
    resolver = _make_resolver(uow=uow)
    assert resolver.relaunch_item_for_rom(1) is None


def test_single_rom_multi_disc_pin_bakes_selected_disc_path():
    """A multi-disc ROM pinned to disc 2 → its single-ROM item bakes disc 2."""
    uow = FakeUnitOfWork()
    _seed_multi_disc(uow, rom_id=1, selected_disc=_DISC2, app_id=555)
    resolver = _make_resolver(uow=uow, disc_resolver=_multi_disc_resolver())
    assert resolver.relaunch_item_for_rom(1) == {
        "app_id": 555,
        "launch_options": f'flatpak run net.retrodeck.retrodeck "{_DISC2_PATH}"',
    }
