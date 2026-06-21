"""Tests for DiscService — get_disc_selection (read) + select_disc (write)."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

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
