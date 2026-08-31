"""Tests for DiscService — get_disc_selection (read) + select_disc (write)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.uow_open_probe import record_uow_open

from domain.disc_selection import Disc
from domain.rom import Rom
from domain.rom_install import RomInstall
from services.disc import DiscService, DiscServiceConfig

_ROM_DIR = "/roms/psx/game"
_DISC1 = "Game (Disc 1).cue"
_DISC2 = "Game (Disc 2).cue"


@contextlib.contextmanager
def uow_unwrap(uow):
    """Open the shared fake UoW to read committed state after the service closed it."""
    with uow as u:
        yield u


def _seed_rom(uow: FakeUnitOfWork, *, rom_id: int, selected_disc: str | None = None) -> None:
    uow.roms.save(
        Rom(
            rom_id=rom_id,
            platform_slug="psx",
            name=f"rom-{rom_id}",
            fs_name=f"rom-{rom_id}",
            shortcut_app_id=42,
            last_synced_at="2026-01-01T00:00:00+00:00",
            selected_disc=selected_disc,
        )
    )


def _seed_install(uow: FakeUnitOfWork, *, rom_id: int, rom_dir: str | None) -> None:
    uow.rom_installs.save(
        RomInstall(
            rom_id=rom_id,
            file_path=f"{_ROM_DIR}/{_DISC1}" if rom_dir else "/roms/psx/single.chd",
            rom_dir=rom_dir,
            platform_slug="psx",
            system="psx",
            installed_at="2026-01-01T00:00:00+00:00",
        )
    )


def _multi_disc_list() -> list[Disc]:
    return [
        Disc(filename=_DISC1, path=f"{_ROM_DIR}/{_DISC1}", label="Disc 1", index=1),
        Disc(filename=_DISC2, path=f"{_ROM_DIR}/{_DISC2}", label="Disc 2", index=2),
    ]


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


@pytest.fixture
def uow_factory(uow) -> FakeUnitOfWorkFactory:
    return FakeUnitOfWorkFactory(uow=uow)


@pytest.fixture
def disc_resolver() -> FakeDiscResolver:
    resolver = FakeDiscResolver()
    resolver.set_discs(_ROM_DIR, _multi_disc_list())
    return resolver


@pytest.fixture
def service(event_loop, uow_factory, disc_resolver) -> DiscService:
    return DiscService(
        config=DiscServiceConfig(
            loop=event_loop,
            logger=logging.getLogger("test_disc"),
            uow_factory=uow_factory,
            disc_resolver=disc_resolver,
            active_core=FakeActiveCoreResolver(default=(None, None)),
        ),
    )


# ── get_disc_selection ─────────────────────────────────────────────────


class TestGetDiscSelection:
    def test_multi_disc_returns_full_descriptor(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, selected_disc=_DISC2)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        result = event_loop.run_until_complete(service.get_disc_selection(1))
        assert result == {
            "multi_disc": True,
            "discs": [
                {"filename": _DISC1, "label": "Disc 1", "index": 1},
                {"filename": _DISC2, "label": "Disc 2", "index": 2},
            ],
            "selected": _DISC2,
            "default": {"kind": "disc", "label": "Disc 1", "filename": _DISC1},
        }

    def test_multi_disc_unpinned_selected_is_none(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, selected_disc=None)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        result = event_loop.run_until_complete(service.get_disc_selection(1))
        assert result["multi_disc"] is True
        assert result["selected"] is None

    def test_single_file_install_not_multi(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1)
        _seed_install(uow, rom_id=1, rom_dir=None)  # single-file, no folder
        result = event_loop.run_until_complete(service.get_disc_selection(1))
        assert result == {"multi_disc": False}

    def test_fewer_than_two_discs_not_multi(self, event_loop, service, uow, disc_resolver):
        # A folder-backed install whose directory enumerates a single disc.
        disc_resolver.set_discs(_ROM_DIR, [_multi_disc_list()[0]])
        _seed_rom(uow, rom_id=1)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        result = event_loop.run_until_complete(service.get_disc_selection(1))
        assert result == {"multi_disc": False}

    def test_not_installed_not_multi(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1)  # rom but no install record
        result = event_loop.run_until_complete(service.get_disc_selection(1))
        assert result == {"multi_disc": False}

    def test_unknown_rom_not_multi(self, event_loop, service):
        result = event_loop.run_until_complete(service.get_disc_selection(999))
        assert result == {"multi_disc": False}

    def test_live_pin_returned_verbatim(self, event_loop, service, uow):
        # A pin whose file is still enumerated is returned as-is (the badge shows
        # exactly what the bake launches).
        _seed_rom(uow, rom_id=1, selected_disc=_DISC2)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        result = event_loop.run_until_complete(service.get_disc_selection(1))
        assert result["selected"] == _DISC2

    def test_stale_pin_down_validated_to_none(self, event_loop, service, uow):
        # A pin whose file is no longer enumerated degrades to None so the badge
        # matches what the bake actually launches (the bake degrades the same
        # stale pin to the default).
        _seed_rom(uow, rom_id=1, selected_disc="Game (Disc 9).cue")
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        result = event_loop.run_until_complete(service.get_disc_selection(1))
        assert result["multi_disc"] is True
        assert result["selected"] is None
        # The disc list itself is unaffected — only the badge degrades.
        assert [d["filename"] for d in result["discs"]] == [_DISC1, _DISC2]


# ── select_disc ────────────────────────────────────────────────────────


class TestSelectDisc:
    def test_pin_happy_path_persists_and_bakes(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, selected_disc=None)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        result = event_loop.run_until_complete(service.select_disc(1, _DISC2))
        assert result["success"] is True
        assert result["selected"] == _DISC2
        # Baked launch_options point at the pinned disc's path.
        assert f"{_ROM_DIR}/{_DISC2}" in result["launch_options"]
        # The pin is persisted via the pin-only write path.
        with uow_unwrap(uow) as u:
            assert u.roms.get(1).selected_disc == _DISC2

    def test_clear_to_default_persists_null(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, selected_disc=_DISC2)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        result = event_loop.run_until_complete(service.select_disc(1, None))
        assert result["success"] is True
        assert result["selected"] is None
        # Default (file_path is disc 1, not an m3u) → disc 1 path baked.
        assert f"{_ROM_DIR}/{_DISC1}" in result["launch_options"]
        with uow_unwrap(uow) as u:
            assert u.roms.get(1).selected_disc is None

    def test_invalid_filename_fails_and_writes_nothing(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1, selected_disc=None)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        result = event_loop.run_until_complete(service.select_disc(1, "Game (Disc 9).cue"))
        assert result == {
            "success": False,
            "reason": "not_found",
            "message": "'Game (Disc 9).cue' is not a disc of ROM 1",
        }
        with uow_unwrap(uow) as u:
            assert u.roms.get(1).selected_disc is None

    def test_not_installed_fails(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1)  # no install record
        result = event_loop.run_until_complete(service.select_disc(1, _DISC2))
        assert result["success"] is False
        assert result["reason"] == "not_installed"
        assert "message" in result

    def test_single_file_install_fails_not_installed(self, event_loop, service, uow):
        _seed_rom(uow, rom_id=1)
        _seed_install(uow, rom_id=1, rom_dir=None)
        result = event_loop.run_until_complete(service.select_disc(1, _DISC2))
        assert result["success"] is False
        assert result["reason"] == "not_installed"

    def test_not_multi_disc_fails_unsupported(self, event_loop, service, uow, disc_resolver):
        disc_resolver.set_discs(_ROM_DIR, [_multi_disc_list()[0]])  # only one disc
        _seed_rom(uow, rom_id=1)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        result = event_loop.run_until_complete(service.select_disc(1, _DISC1))
        assert result["success"] is False
        assert result["reason"] == "unsupported"

    def test_unknown_rom_fails(self, event_loop, service):
        result = event_loop.run_until_complete(service.select_disc(999, _DISC1))
        assert result["success"] is False
        assert result["reason"] == "not_installed"


# ── transaction boundary ───────────────────────────────────────────────


def _retire_between_transactions(uow: FakeUnitOfWork, disc_resolver: FakeDiscResolver, rom_id: int, *, drop_rom: bool):
    """Delete the ROM row (or only its install) while the picker enumerates.

    Enumeration is the window between ``select_disc``'s read transaction and
    its write transaction — the moment a background sync, a finishing download
    or the removed-game cleanup can retire the ROM from its own connection.
    """
    enumerate_discs = disc_resolver.enumerate_discs

    def retiring(install):
        with uow:
            if drop_rom:
                uow.roms.delete(rom_id)
            else:
                uow.rom_installs.delete(rom_id)
        return enumerate_discs(install)

    disc_resolver.enumerate_discs = retiring


def _relocate_between_transactions(uow: FakeUnitOfWork, disc_resolver: FakeDiscResolver, rom_id: int, *, rom_dir: str):
    """Move the install to a different directory while the picker enumerates.

    The same window as :func:`_retire_between_transactions`, with the install
    replaced rather than deleted — a RetroDECK-home migration relocating the
    ROM. The enumerated disc list still describes the old directory.
    """
    enumerate_discs = disc_resolver.enumerate_discs

    def relocating(install):
        discs = enumerate_discs(install)
        with uow:
            _seed_install(uow, rom_id=rom_id, rom_dir=rom_dir)
        return discs

    disc_resolver.enumerate_discs = relocating


def _unfold_install_between_transactions(uow: FakeUnitOfWork, disc_resolver: FakeDiscResolver, rom_id: int) -> None:
    """Replace the folder-backed install with a single-file one while enumerating.

    The same window as :func:`_retire_between_transactions`, with the install
    re-installed as a single file (``rom_dir`` NULL) — the shape the first
    transaction refuses outright, arriving after it has already admitted the ROM.
    """
    enumerate_discs = disc_resolver.enumerate_discs

    def unfolding(install):
        discs = enumerate_discs(install)
        with uow:
            _seed_install(uow, rom_id=rom_id, rom_dir=None)
        return discs

    disc_resolver.enumerate_discs = unfolding


class TestTransactionBoundary:
    """Enumeration and the bake run between transactions, never inside one.

    ``enumerate_discs`` lists the install directory, and the bake resolves the
    ROM's active core through a seam that opens its own UoW. A UoW takes
    SQLite's non-reentrant ``BEGIN IMMEDIATE`` write lock, so file I/O held
    inside one stalls every other writer in the plugin and a nested open
    self-deadlocks (CONTEXT.md → Unit of Work, #1779). ``FakeUnitOfWork``
    shares no connection, so what a test can see is the ordering.
    """

    def test_get_disc_selection_enumerates_outside_the_uow(self, event_loop, service, uow, disc_resolver):
        _seed_rom(uow, rom_id=1, selected_disc=_DISC2)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        open_at_enumerate = record_uow_open(uow, disc_resolver, "enumerate_discs")

        result = event_loop.run_until_complete(service.get_disc_selection(1))

        assert result["multi_disc"] is True
        assert open_at_enumerate == [False]

    def test_select_disc_enumerates_and_bakes_outside_the_uow(self, event_loop, service, uow, disc_resolver):
        _seed_rom(uow, rom_id=1, selected_disc=None)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        open_at_enumerate = record_uow_open(uow, disc_resolver, "enumerate_discs")
        open_at_bake = record_uow_open(uow, disc_resolver, "resolve_bake_path")

        result = event_loop.run_until_complete(service.select_disc(1, _DISC2))

        assert result["success"] is True
        assert open_at_enumerate == [False]
        assert open_at_bake == [False]

    def test_rom_retired_between_transactions_fails_not_installed(self, event_loop, service, uow, disc_resolver):
        _seed_rom(uow, rom_id=1, selected_disc=None)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        _retire_between_transactions(uow, disc_resolver, 1, drop_rom=True)

        result = event_loop.run_until_complete(service.select_disc(1, _DISC2))

        assert result["success"] is False
        assert result["reason"] == "not_installed"
        assert "message" in result

    def test_bake_resolves_over_the_install_the_discs_were_enumerated_from(
        self, event_loop, service, uow, disc_resolver
    ):
        _seed_rom(uow, rom_id=1, selected_disc=None)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        _relocate_between_transactions(uow, disc_resolver, 1, rom_dir="/roms/psx/moved")

        result = event_loop.run_until_complete(service.select_disc(1, _DISC2))

        assert result["success"] is True
        # The bake resolves the pin over the enumerated list, so it must see the
        # install that list came from — the relocated one was never enumerated.
        assert disc_resolver.calls[-1] == (_ROM_DIR, _DISC2)

    def test_install_retired_between_transactions_fails_not_installed(self, event_loop, service, uow, disc_resolver):
        _seed_rom(uow, rom_id=1, selected_disc=None)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        _retire_between_transactions(uow, disc_resolver, 1, drop_rom=False)

        result = event_loop.run_until_complete(service.select_disc(1, _DISC2))

        assert result["success"] is False
        assert result["reason"] == "not_installed"
        # Nothing was pinned on the surviving row.
        with uow_unwrap(uow) as u:
            assert u.roms.get(1).selected_disc is None

    def test_install_unfolded_between_transactions_fails_not_installed(self, event_loop, service, uow, disc_resolver):
        # A folder-backed install re-installed as a single file in the window is
        # the shape the read transaction refuses up front; the write transaction
        # applies the same admission rather than pinning a disc onto a ROM that
        # no longer has a disc folder.
        _seed_rom(uow, rom_id=1, selected_disc=None)
        _seed_install(uow, rom_id=1, rom_dir=_ROM_DIR)
        _unfold_install_between_transactions(uow, disc_resolver, 1)

        result = event_loop.run_until_complete(service.select_disc(1, _DISC2))

        assert result["success"] is False
        assert result["reason"] == "not_installed"
        with uow_unwrap(uow) as u:
            assert u.roms.get(1).selected_disc is None
