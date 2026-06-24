"""The disc resolver is honored at all three launch-bake sites.

Each site re-bakes ``launch_options`` from a per-ROM path. With a multi-disc ROM
pinned to disc 2, the baked path must be disc 2's path — proving the resolver is
threaded through. A non-multi-disc ROM (the FakeDiscResolver with no discs
seeded for its directory) resolves to its own ``file_path`` unchanged, which the
existing per-site happy-path tests already assert; here we pin the disc-aware
behavior.

Bake sites covered:
  * ``services.library.sync_orchestrator`` — the ``installed_paths`` map.
  * ``services.downloads`` — ``_finalize_download_complete`` (via ``_resolve_bound_app_id``).

The migration relaunch path (``services.migration._build_relaunch_items``) and
the startup reconcile both bake through the shared ``RelaunchOptionsResolver``;
its disc-pin behavior is pinned in ``test_relaunch_options_resolver.py``.
"""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from fakes.fake_active_core_resolver import FakeActiveCoreResolver
from fakes.fake_disc_resolver import FakeDiscResolver
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from domain.disc_selection import Disc
from domain.rom import Rom
from domain.rom_install import RomInstall

_ROM_DIR = "/roms/psx/game-1"
_DISC1 = "Game (Disc 1).cue"
_DISC2 = "Game (Disc 2).cue"
_DISC1_PATH = f"{_ROM_DIR}/{_DISC1}"
_DISC2_PATH = f"{_ROM_DIR}/{_DISC2}"


def _discs() -> list[Disc]:
    return [
        Disc(filename=_DISC1, path=_DISC1_PATH, label="Disc 1", index=1),
        Disc(filename=_DISC2, path=_DISC2_PATH, label="Disc 2", index=2),
    ]


def _seed_multi_disc(uow: FakeUnitOfWork, *, rom_id: int, selected_disc: str | None, app_id: int | None = 99) -> None:
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


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def disc_resolver() -> FakeDiscResolver:
    resolver = FakeDiscResolver()
    resolver.set_discs(_ROM_DIR, _discs())
    return resolver


# ── sync_orchestrator bake site ──────────────────────────────────────────


class TestSyncOrchestratorBakeSite:
    def _orchestrator(self, uow_factory, disc_resolver):
        from services.library.sync_orchestrator import SyncOrchestrator, SyncOrchestratorConfig

        return SyncOrchestrator(
            config=SyncOrchestratorConfig(
                settings={},
                loop=MagicMock(),
                logger=logging.getLogger("test_disc_bake"),
                plugin_dir="/plugin",
                emit=MagicMock(),
                clock=FakeClock(),
                uuid_gen=FakeUuidGen(),
                sleeper=FakeSleeper(),
                uow_factory=uow_factory,
                sync_state_box=MagicMock(),
                fetcher=MagicMock(),
                reporter=MagicMock(),
                artwork=MagicMock(),
                active_core=FakeActiveCoreResolver(default=(None, None)),
                disc_resolver=disc_resolver,
            )
        )

    def test_scan_installed_paths_honors_pin(self, disc_resolver):
        uow = FakeUnitOfWork()
        _seed_multi_disc(uow, rom_id=1, selected_disc=_DISC2)
        orch = self._orchestrator(FakeUnitOfWorkFactory(uow=uow), disc_resolver)
        assert orch._scan_installed_paths() == {1: _DISC2_PATH}

    def test_read_installed_paths_honors_pin(self, disc_resolver):
        uow = FakeUnitOfWork()
        _seed_multi_disc(uow, rom_id=1, selected_disc=_DISC2)
        orch = self._orchestrator(FakeUnitOfWorkFactory(uow=uow), disc_resolver)
        assert orch._read_installed_paths({1}) == {1: _DISC2_PATH}

    def test_scan_installed_paths_unpinned_defaults_to_disc_1(self, disc_resolver):
        uow = FakeUnitOfWork()
        _seed_multi_disc(uow, rom_id=1, selected_disc=None)
        orch = self._orchestrator(FakeUnitOfWorkFactory(uow=uow), disc_resolver)
        # file_path is disc 1 (not an m3u) → default resolves to disc 1.
        assert orch._scan_installed_paths() == {1: _DISC1_PATH}


# ── downloads bake site ──────────────────────────────────────────────────


class TestDownloadsBakeSite:
    def _service(self, uow_factory, disc_resolver):
        from services.downloads import DownloadService, DownloadServiceConfig

        return DownloadService(
            config=DownloadServiceConfig(
                romm_api=MagicMock(),
                download_file_store=MagicMock(),
                resolve_system=lambda platform_slug, platform_fs_slug=None: platform_slug,
                loop=MagicMock(),
                logger=logging.getLogger("test_disc_bake"),
                emit=MagicMock(),
                clock=FakeClock(),
                sleeper=FakeSleeper(),
                retrodeck_paths=MagicMock(),
                active_core=FakeActiveCoreResolver(default=(None, None)),
                disc_resolver=disc_resolver,
                m3u_support=lambda system_name: False,
                uow_factory=uow_factory,
            )
        )

    def test_resolve_bound_app_id_returns_pinned_bake_path(self, disc_resolver):
        uow = FakeUnitOfWork()
        _seed_multi_disc(uow, rom_id=1, selected_disc=_DISC2, app_id=1234)
        svc = self._service(FakeUnitOfWorkFactory(uow=uow), disc_resolver)
        app_id, core_so, bake_path = svc._resolve_bound_app_id(1, _DISC1_PATH)
        assert app_id == 1234
        assert core_so is None
        assert bake_path == _DISC2_PATH

    def test_resolve_bound_app_id_unpinned_defaults_to_disc_1(self, disc_resolver):
        uow = FakeUnitOfWork()
        _seed_multi_disc(uow, rom_id=1, selected_disc=None, app_id=1234)
        svc = self._service(FakeUnitOfWorkFactory(uow=uow), disc_resolver)
        _app_id, _core_so, bake_path = svc._resolve_bound_app_id(1, _DISC1_PATH)
        assert bake_path == _DISC1_PATH
