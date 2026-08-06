"""Tests for RomRemovalService — ROM file deletion and ``rom_installs`` cleanup."""

import asyncio
import logging
import os
import shutil
import sys

import pytest
from fakes.fake_download_queue_cleanup import FakeDownloadQueueCleanup
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths
from fakes.fake_rom_file_store import FakeRomFileStore
from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory
from fakes.system_time import FakeClock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "py_modules"))
sys.path.insert(0, os.path.dirname(__file__))

# conftest.py patches decky before this import
from adapters.recovery_bundle import RecoveryBundleAdapter
from adapters.rom_files import RomFileAdapter
from domain.prune import BundleReadmeContext
from domain.rom import Rom
from domain.rom_install import RomInstall
from services.rom_removal import RomRemovalService, RomRemovalServiceConfig

# Synthetic roms-base path used by the fake fs throughout this module.
_ROMS_BASE = "/retrodeck/roms"


@pytest.fixture
def logger():
    return logging.getLogger("test_rom_removal")


@pytest.fixture
def queue_cleanup() -> FakeDownloadQueueCleanup:
    return FakeDownloadQueueCleanup()


@pytest.fixture
def rom_files() -> FakeRomFileStore:
    return FakeRomFileStore()


@pytest.fixture
def uow() -> FakeUnitOfWork:
    return FakeUnitOfWork()


class RecordingEmitter:
    """Records every ``(event, payload)`` a service emits."""

    def __init__(self) -> None:
        self.events: list[tuple[str, object]] = []

    async def __call__(self, event: str, /, *args: object) -> None:
        self.events.append((event, args[0] if args else None))

    def payloads(self, event: str) -> list[object]:
        return [payload for name, payload in self.events if name == event]


@pytest.fixture
def emitter() -> RecordingEmitter:
    return RecordingEmitter()


@pytest.fixture
def service(logger, queue_cleanup, rom_files, uow, emitter):
    return RomRemovalService(
        config=RomRemovalServiceConfig(
            logger=logger,
            loop=asyncio.new_event_loop(),
            clock=FakeClock(),
            emit=emitter,
            rom_file_store=rom_files,
            retrodeck_paths=FakeRetroDeckPaths(roms=_ROMS_BASE),
            download_queue_cleanup=queue_cleanup,
            uow_factory=FakeUnitOfWorkFactory(uow),
        ),
    )


@pytest.fixture(autouse=True)
async def _sync_loop(service):
    """Keep service loop in sync with the running event loop."""
    service._loop = asyncio.get_event_loop()


def _make_rom(rom_id: int, *, platform_slug: str = "n64", bound: bool = True) -> Rom:
    """Build the FK-parent ``roms`` row so a child ``rom_installs`` write commits.

    Bound rows carry ``shortcut_app_id = 1000 + rom_id`` (a live Steam
    shortcut); ``bound=False`` leaves it ``None`` (no shortcut to reset).
    """
    return Rom(
        rom_id=rom_id,
        platform_slug=platform_slug,
        name=f"Game {rom_id}",
        fs_name=f"game_{rom_id}.z64",
        shortcut_app_id=(1000 + rom_id) if bound else None,
        last_synced_at="2025-01-01T00:00:00",
    )


def _make_install(rom_id: int, *, file_path: str, rom_dir: str | None = None, system: str = "n64") -> RomInstall:
    return RomInstall.mark_installed(
        rom_id=rom_id,
        file_path=file_path,
        rom_dir=rom_dir,
        platform_slug=system,
        system=system,
        installed_at="2025-01-01T00:00:00",
    )


def _installed(uow: FakeUnitOfWork, rom_id: int) -> RomInstall:
    """Read a seeded install record back, failing the test if the seed did not commit."""
    install = uow.rom_installs.get(rom_id)
    assert install is not None
    return install


def _seed_install(uow: FakeUnitOfWork, install: RomInstall, *, platform_slug: str = "n64") -> None:
    """Seed the FK-parent Rom THEN its install record, in one commit."""
    with uow:
        uow.roms.save(_make_rom(install.rom_id, platform_slug=platform_slug))
        uow.rom_installs.save(install)


class TestDeleteRomFiles:
    def test_deletes_single_file(self, service, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"\x00" * 100

        service._delete_rom_files(_make_install(1, file_path=rom_path))

        assert rom_path not in rom_files.files
        assert rom_files.remove_file_calls == [rom_path]
        # A single-file ROM has no rom_dir, so no directory tree is ever removed.
        assert rom_files.remove_tree_calls == []

    def test_deletes_rom_dir(self, service, rom_files):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.cue"] = b"cue"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100

        service._delete_rom_files(_make_install(1, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"))

        assert f"{rom_dir}/disc1.cue" not in rom_files.files
        assert f"{rom_dir}/disc1.bin" not in rom_files.files
        assert rom_files.remove_tree_calls == [rom_dir]

    def test_single_file_owns_no_dir_so_system_dir_not_removed(self, service, rom_files):
        """A single-file ROM (``rom_dir`` is ``None``) lives in the shared ``<roms>/<system>`` dir.

        With no ``rom_dir`` set, the directory tree is never removed — only the
        launch file is deleted. Removing the shared system dir would wipe the
        whole platform's folder.
        """
        system_dir = f"{_ROMS_BASE}/n64"
        rom_path = f"{system_dir}/game.z64"
        sibling = f"{system_dir}/other_game.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        rom_files.files[sibling] = b"\x00" * 100
        rom_files.dirs.add(system_dir)

        service._delete_rom_files(_make_install(1, file_path=rom_path, rom_dir=None))

        assert rom_path not in rom_files.files
        assert sibling in rom_files.files  # the platform's other ROM survives
        assert system_dir in rom_files.dirs  # the system dir itself survives
        assert rom_files.remove_tree_calls == []

    def test_single_file_record_pointing_at_nested_directory_fails_closed(self, service, rom_files):
        nested = f"{_ROMS_BASE}/n64/shared-content"
        rom_files.dirs.add(nested)
        rom_files.files[f"{nested}/other.z64"] = b"keep"

        install = _make_install(1, file_path=nested, rom_dir=None)
        with pytest.raises(ValueError, match="Expected installed ROM file"):
            service._delete_rom_files(install)

        assert f"{nested}/other.z64" in rom_files.files
        assert rom_files.remove_tree_calls == []

    def test_filesystem_only_removal_leaves_install_and_rom_rows(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(7, file_path=rom_path))

        result = service.delete_rom_files(7)

        assert result["success"] is True
        assert uow.roms.get(7) is not None
        assert uow.rom_installs.get(7) is not None

    def test_refuses_file_outside_roms_dir(self, service, rom_files):
        evil = "/evil/important.txt"
        rom_files.files[evil] = b"do not delete"

        install = _make_install(1, file_path=evil, rom_dir=None)
        with pytest.raises(ValueError, match="outside roms directory"):
            service._delete_rom_files(install)

        assert evil in rom_files.files
        assert rom_files.remove_file_calls == []
        assert rom_files.remove_tree_calls == []

    def test_refuses_rom_dir_outside_roms_dir(self, service, rom_files):
        evil_dir = "/evil/dir"
        rom_files.files[f"{evil_dir}/file.txt"] = b"important"

        install = _make_install(1, file_path="", rom_dir=evil_dir)
        with pytest.raises(ValueError, match="outside roms directory"):
            service._delete_rom_files(install)

        assert f"{evil_dir}/file.txt" in rom_files.files
        assert rom_files.remove_tree_calls == []

    def test_missing_file_no_crash(self, service):
        # File doesn't exist — should not raise and should not call any I/O
        service._delete_rom_files(_make_install(1, file_path=f"{_ROMS_BASE}/n64/gone.z64"))

    def test_empty_paths_no_crash(self, service):
        # No file_path, no rom_dir
        service._delete_rom_files(_make_install(1, file_path="", rom_dir=None))

    def test_sealed_file_replacement_is_retained_at_mutation_time(self, tmp_path, logger):
        roms = tmp_path / "roms"
        rom_path = roms / "n64" / "game.z64"
        rom_path.parent.mkdir(parents=True)
        rom_path.write_bytes(b"sealed")
        recovery = RecoveryBundleAdapter(
            user_home=str(tmp_path),
            package_name="decky-romm-sync",
            plugin_version="test",
        )
        bundle = recovery.seal_bundle(
            "Game_2026-07-24_romfile",
            {"roms": [{"rom_id": 1}]},
            [{"source_path": str(rom_path), "safe_root": str(roms), "kind": "installed_rom", "rom_id": 1}],
            BundleReadmeContext(
                bundle_id="Game_2026-07-24_romfile",
                created_at="2026-07-24T12:00:00+00:00",
                games=[],
                playtime_lines=[],
            ),
            "playtime",
        )
        claims = recovery.source_claims(bundle)["claims"]
        rom_path.unlink()
        rom_path.write_bytes(b"replacement")
        uow = FakeUnitOfWork()
        _seed_install(uow, _make_install(1, file_path=str(rom_path)))
        real_service = RomRemovalService(
            config=RomRemovalServiceConfig(
                logger=logger,
                loop=asyncio.new_event_loop(),
                clock=FakeClock(),
                emit=RecordingEmitter(),
                rom_file_store=RomFileAdapter(),
                retrodeck_paths=FakeRetroDeckPaths(roms=str(roms)),
                download_queue_cleanup=None,
                uow_factory=FakeUnitOfWorkFactory(uow),
            )
        )

        result = real_service.delete_rom_files(1, claims)

        assert result["success"] is False
        assert "identity changed" in result["message"]
        assert rom_path.read_bytes() == b"replacement"

    def test_preopened_rom_writer_prevents_installed_file_deletion(self, tmp_path, logger):
        roms = tmp_path / "roms"
        rom_path = roms / "n64" / "game.z64"
        rom_path.parent.mkdir(parents=True)
        rom_path.write_bytes(b"installed")
        uow = FakeUnitOfWork()
        _seed_install(uow, _make_install(1, file_path=str(rom_path)))
        real_service = RomRemovalService(
            config=RomRemovalServiceConfig(
                logger=logger,
                loop=asyncio.new_event_loop(),
                clock=FakeClock(),
                emit=RecordingEmitter(),
                rom_file_store=RomFileAdapter(),
                retrodeck_paths=FakeRetroDeckPaths(roms=str(roms)),
                download_queue_cleanup=None,
                uow_factory=FakeUnitOfWorkFactory(uow),
            )
        )
        writer = os.open(rom_path, os.O_WRONLY)
        try:
            result = real_service.delete_rom_files(1)
        finally:
            os.close(writer)

        assert result["success"] is False
        assert "active writer" in result["message"]
        assert rom_path.read_bytes() == b"installed"

    def test_selected_directory_child_change_is_retained_at_mutation_time(self, tmp_path, logger):
        roms = tmp_path / "roms"
        rom_dir = roms / "psx" / "Game"
        rom_dir.mkdir(parents=True)
        child = rom_dir / "disc.bin"
        child.write_bytes(b"sealed")
        recovery = RecoveryBundleAdapter(user_home=str(tmp_path), package_name="decky-romm-sync", plugin_version="test")
        bundle = recovery.seal_bundle(
            "Game_2026-07-24_romdir",
            {"roms": [{"rom_id": 1}]},
            [{"source_path": str(rom_dir), "safe_root": str(roms), "kind": "installed_rom", "rom_id": 1}],
            BundleReadmeContext(
                bundle_id="Game_2026-07-24_romdir",
                created_at="2026-07-24T12:00:00+00:00",
                games=[],
                playtime_lines=[],
            ),
            "playtime",
        )
        claims = recovery.source_claims(bundle)["claims"]
        child.write_bytes(b"replacement")
        uow = FakeUnitOfWork()
        _seed_install(uow, _make_install(1, file_path=str(child), rom_dir=str(rom_dir), system="psx"))
        real_service = RomRemovalService(
            config=RomRemovalServiceConfig(
                logger=logger,
                loop=asyncio.new_event_loop(),
                clock=FakeClock(),
                emit=RecordingEmitter(),
                rom_file_store=RomFileAdapter(),
                retrodeck_paths=FakeRetroDeckPaths(roms=str(roms)),
                download_queue_cleanup=None,
                uow_factory=FakeUnitOfWorkFactory(uow),
            )
        )

        result = real_service.delete_rom_files(1, claims)

        assert result["success"] is False
        assert "subtree changed" in result["message"]
        assert child.read_bytes() == b"replacement"

    @pytest.mark.parametrize("claims", [None, {}], ids=["no-bundle", "bundle-without-this-source"])
    def test_directory_replacement_after_a_final_claim_is_retained(self, tmp_path, logger, monkeypatch, claims):
        """Either discipline refuses a source swapped out between its final claim and the mutation."""
        roms = tmp_path / "roms"
        rom_dir = roms / "psx" / "Game"
        rom_dir.mkdir(parents=True)
        (rom_dir / "disc.bin").write_bytes(b"original")
        replacement = roms / "psx" / "Replacement"
        replacement.mkdir()
        (replacement / "disc.bin").write_bytes(b"replacement")
        store = RomFileAdapter()
        original_claim = store.claim_source

        def claim_then_replace(path: str, safe_root: str, *, digest: bool = True):
            claim = original_claim(path, safe_root, digest=digest)
            shutil.rmtree(path)
            replacement.rename(path)
            return claim

        monkeypatch.setattr(store, "claim_source", claim_then_replace)
        uow = FakeUnitOfWork()
        _seed_install(
            uow,
            _make_install(1, file_path=str(rom_dir / "disc.bin"), rom_dir=str(rom_dir), system="psx"),
        )
        real_service = RomRemovalService(
            config=RomRemovalServiceConfig(
                logger=logger,
                loop=asyncio.new_event_loop(),
                clock=FakeClock(),
                emit=RecordingEmitter(),
                rom_file_store=store,
                retrodeck_paths=FakeRetroDeckPaths(roms=str(roms)),
                download_queue_cleanup=None,
                uow_factory=FakeUnitOfWorkFactory(uow),
            )
        )

        result = real_service.delete_rom_files(1, claims)

        assert result["success"] is False
        assert "identity changed" in result["message"]
        assert (rom_dir / "disc.bin").read_bytes() == b"replacement"


class TestRemoveRom:
    @pytest.mark.asyncio
    async def test_removes_file_and_clears_install_record(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(42, file_path=rom_path))

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert rom_path not in rom_files.files
        assert uow.rom_installs.get(42) is None
        assert uow.committed is True

    @pytest.mark.asyncio
    async def test_records_empty_applied_launch_options_for_bound_rom(self, service, uow, rom_files):
        # The frontend resets the kept shortcut's launch command to "" on uninstall
        # (#1146); the backend records "" so the next sync skips it (#1383).
        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(42, file_path=rom_path))
        with uow:
            uow.roms.set_applied_launch_options(42, "flatpak run net.retrodeck.retrodeck /zelda.z64")

        await service.remove_rom(42)

        with uow:
            rom = uow.roms.get(42)
        assert rom is not None
        assert rom.applied_launch_options == ""

    @pytest.mark.asyncio
    async def test_does_not_record_applied_for_unbound_rom(self, service, uow, rom_files):
        # An unbound ROM has no shortcut to reset — the recording is guarded on the
        # binding, so its applied state stays untouched (unknown).
        rom_path = f"{_ROMS_BASE}/n64/unbound.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(50, bound=False))
            uow.rom_installs.save(_make_install(50, file_path=rom_path))

        await service.remove_rom(50)

        with uow:
            rom = uow.roms.get(50)
        assert rom is not None
        assert rom.applied_launch_options is None

    @pytest.mark.asyncio
    async def test_returns_error_if_not_installed(self, service):
        result = await service.remove_rom(999)
        assert result["success"] is False
        assert "not installed" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_accepts_string_rom_id(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(7, file_path=rom_path))

        result = await service.remove_rom("7")

        assert result["success"] is True
        assert uow.rom_installs.get(7) is None

    @pytest.mark.asyncio
    async def test_file_already_gone_still_deletes_record(self, service, uow):
        """Edge: the file is already gone on disk → the install record is still dropped."""
        _seed_install(
            uow,
            _make_install(42, file_path=f"{_ROMS_BASE}/n64/gone.z64"),
        )

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert uow.rom_installs.get(42) is None

    @pytest.mark.asyncio
    async def test_retains_playtime_saves_and_roms_row(self, service, uow, rom_files):
        """RETENTION (ADR-0007 / D1): uninstall drops only files + the install record.

        Playtime, the save-sync state, and the ``roms`` identity row all survive.
        """
        from domain.playtime import Playtime
        from domain.rom_save_sync_state import RomSaveSyncState

        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100

        playtime = Playtime(total_seconds=3600, session_count=2)
        save_state = RomSaveSyncState(active_slot="default", slot_confirmed=True)
        with uow:
            uow.roms.save(_make_rom(42))
            uow.rom_installs.save(_make_install(42, file_path=rom_path))
            uow.playtime.save(42, playtime)
            uow.rom_save_sync_states.save(42, save_state)

        result = await service.remove_rom(42)

        assert result["success"] is True
        # Only the install record is gone.
        assert uow.rom_installs.get(42) is None
        # Identity, playtime, and save-sync state all survive the uninstall.
        assert uow.roms.get(42) is not None
        surviving_playtime = uow.playtime.get(42)
        assert surviving_playtime is not None
        assert surviving_playtime.total_seconds == 3600
        surviving_save = uow.rom_save_sync_states.get(42)
        assert surviving_save is not None
        assert surviving_save.active_slot == "default"
        assert uow.committed is True

    @pytest.mark.asyncio
    async def test_removes_rom_dir(self, service, uow, rom_files):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/FF7.m3u"] = b"disc1.cue"
        rom_files.files[f"{rom_dir}/disc1.cue"] = b"cue"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        # Mark the parent system dir as existing so we can assert it's preserved.
        rom_files.dirs.add(f"{_ROMS_BASE}/psx")
        _seed_install(
            uow,
            _make_install(42, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"),
            platform_slug="psx",
        )

        result = await service.remove_rom(42)

        assert result["success"] is True
        # rom_dir gone
        assert all(not p.startswith(rom_dir + "/") for p in rom_files.files)
        # Parent system dir still tracked
        assert f"{_ROMS_BASE}/psx" in rom_files.dirs

    @pytest.mark.asyncio
    async def test_path_traversal_rejected_preserves_install_record(self, service, uow, rom_files):
        evil = "/etc/passwd"
        rom_files.files[evil] = b"root:x:0:0"
        _seed_install(uow, _make_install(99, file_path=evil, rom_dir=None))

        result = await service.remove_rom(99)

        assert result == {
            "success": False,
            "reason": "unknown",
            "message": "Failed to delete ROM files",
        }
        assert evil in rom_files.files  # not deleted (outside roms dir)
        assert uow.rom_installs.get(99) is not None

    @pytest.mark.asyncio
    async def test_removes_nested_single_file_entry(self, service, uow, rom_files):
        """Nested-single-file installs (#226): the resolved filename is in file_path; rom_dir is None (no folder)."""
        system_dir = f"{_ROMS_BASE}/dc"
        rom_path = f"{system_dir}/Resident Evil.chd"
        rom_files.files[rom_path] = b"\x00" * 100
        rom_files.dirs.add(system_dir)
        _seed_install(
            uow,
            _make_install(42, file_path=rom_path, rom_dir=None, system="dc"),
            platform_slug="dc",
        )

        result = await service.remove_rom(42)

        assert result["success"] is True
        assert rom_path not in rom_files.files
        # Parent system dir still tracked
        assert system_dir in rom_files.dirs
        assert uow.rom_installs.get(42) is None


class TestUninstallAllRoms:
    @pytest.mark.asyncio
    async def test_removes_all_installed(self, service, uow, rom_files):
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        rom_files.files[file_a] = b"\x00" * 100
        rom_files.files[file_b] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2))
            uow.rom_installs.save(_make_install(1, file_path=file_a))
            uow.rom_installs.save(_make_install(2, file_path=file_b))

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["removed_count"] == 2
        assert file_a not in rom_files.files
        assert file_b not in rom_files.files
        assert list(uow.rom_installs.iter_all()) == []

    @pytest.mark.asyncio
    async def test_clears_records_even_if_files_missing(self, service, uow):
        _seed_install(uow, _make_install(1, file_path=f"{_ROMS_BASE}/n64/nonexistent.z64"))

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert list(uow.rom_installs.iter_all()) == []

    @pytest.mark.asyncio
    async def test_records_empty_applied_for_each_bound_deleted_rom(self, service, uow, rom_files):
        # The frontend resets each kept shortcut's launch command to "" for the
        # returned app_ids (#1146); the backend records "" so the next sync skips
        # each now-correct shortcut (#1383).
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        rom_files.files[file_a] = b"\x00" * 100
        rom_files.files[file_b] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2))
            uow.rom_installs.save(_make_install(1, file_path=file_a))
            uow.rom_installs.save(_make_install(2, file_path=file_b))
            uow.roms.set_applied_launch_options(1, "flatpak run … /game_a.z64")
            uow.roms.set_applied_launch_options(2, "flatpak run … /game_b.z64")

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        with uow:
            assert uow.roms.get(1).applied_launch_options == ""
            assert uow.roms.get(2).applied_launch_options == ""

    @pytest.mark.asyncio
    async def test_handles_empty_state(self, service, uow):
        _ = uow
        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 0

    @pytest.mark.asyncio
    async def test_retains_playtime_and_roms_rows(self, service, uow, rom_files):
        """RETENTION (ADR-0007 / D1): bulk uninstall drops only files + install records.

        Identity rows and playtime survive for every ROM.
        """
        from domain.playtime import Playtime

        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        rom_files.files[file_a] = b"\x00" * 100
        rom_files.files[file_b] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2))
            uow.rom_installs.save(_make_install(1, file_path=file_a))
            uow.rom_installs.save(_make_install(2, file_path=file_b))
            uow.playtime.save(1, Playtime(total_seconds=100, session_count=1))
            uow.playtime.save(2, Playtime(total_seconds=200, session_count=1))

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert list(uow.rom_installs.iter_all()) == []
        # Identity + playtime survive the bulk uninstall.
        assert uow.roms.get(1) is not None
        assert uow.roms.get(2) is not None
        pt1 = uow.playtime.get(1)
        pt2 = uow.playtime.get(2)
        assert pt1 is not None and pt1.total_seconds == 100
        assert pt2 is not None and pt2.total_seconds == 200
        assert uow.committed is True

    @pytest.mark.asyncio
    async def test_deletes_rom_directories(self, service, uow, rom_files):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        _seed_install(
            uow,
            _make_install(1, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"),
            platform_slug="psx",
        )

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert result["removed_count"] == 1
        assert all(not p.startswith(rom_dir + "/") for p in rom_files.files)

    @pytest.mark.asyncio
    async def test_outside_roms_dir_is_partial_failure_and_preserves_record(self, service, uow, rom_files):
        good_file = f"{_ROMS_BASE}/n64/game_a.z64"
        rom_files.files[good_file] = b"\x00" * 100
        bad_file = "/outside/game_b.z64"
        rom_files.files[bad_file] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2, platform_slug="snes"))
            uow.rom_installs.save(_make_install(1, file_path=good_file))
            uow.rom_installs.save(_make_install(2, file_path=bad_file, rom_dir=None, system="snes"))

        result = await service.uninstall_all_roms()
        assert result["success"] is False
        assert result["removed_count"] == 1
        assert len(result["errors"]) == 1
        assert good_file not in rom_files.files
        assert bad_file in rom_files.files  # not deleted (outside roms dir)
        assert [install.rom_id for install in uow.rom_installs.iter_all()] == [2]

    @pytest.mark.asyncio
    async def test_partial_failure_reports_errors_and_not_success(self, service, uow, rom_files):
        """Bad path: one of three deletions raises OSError → ``success`` is False, the failing record survives."""
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        file_c = f"{_ROMS_BASE}/n64/game_c.z64"
        for p in (file_a, file_b, file_c):
            rom_files.files[p] = b"\x00" * 100
        rom_files.remove_file_failures.add(file_b)
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2))
            uow.roms.save(_make_rom(3))
            uow.rom_installs.save(_make_install(1, file_path=file_a))
            uow.rom_installs.save(_make_install(2, file_path=file_b))
            uow.rom_installs.save(_make_install(3, file_path=file_c))

        result = await service.uninstall_all_roms()

        assert result["success"] is False
        assert result["removed_count"] == 2
        assert len(result["errors"]) == 1
        assert result["errors"][0]["rom_id"] == "2"
        assert "game_b.z64" in result["errors"][0]["error"]
        # Records for successful deletions are cleared; the failing entry survives so the user can retry.
        assert uow.rom_installs.get(1) is None
        assert uow.rom_installs.get(2) is not None
        assert uow.rom_installs.get(3) is None

    @pytest.mark.asyncio
    async def test_all_success_returns_empty_errors(self, service, uow, rom_files):
        """Happy path: all 3 deletions succeed → ``success`` is True and ``errors`` is empty."""
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        file_c = f"{_ROMS_BASE}/n64/game_c.z64"
        for p in (file_a, file_b, file_c):
            rom_files.files[p] = b"\x00" * 100
        with uow:
            for rid, fp in ((1, file_a), (2, file_b), (3, file_c)):
                uow.roms.save(_make_rom(rid))
                uow.rom_installs.save(_make_install(rid, file_path=fp))

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["removed_count"] == 3
        assert result["errors"] == []

    @pytest.mark.asyncio
    async def test_empty_state_returns_success_with_empty_errors(self, service, uow):
        """Edge: no installed ROMs → ``success`` is True and ``errors`` is empty."""
        _ = uow
        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["removed_count"] == 0
        assert result["errors"] == []


class TestUninstallAllRomsAppIds:
    """The ``app_ids`` field the frontend uses to reset kept shortcuts' launch_options (#1146)."""

    @pytest.mark.asyncio
    async def test_returns_bound_app_ids_for_deleted_roms(self, service, uow, rom_files):
        """Happy path: each deleted ROM's bound ``shortcut_app_id`` is returned so the frontend can reset it."""
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        rom_files.files[file_a] = b"\x00" * 100
        rom_files.files[file_b] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(1))
            uow.roms.save(_make_rom(2))
            uow.rom_installs.save(_make_install(1, file_path=file_a))
            uow.rom_installs.save(_make_install(2, file_path=file_b))

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert sorted(result["app_ids"]) == [1001, 1002]

    @pytest.mark.asyncio
    async def test_omits_app_id_for_unbound_rom(self, service, uow, rom_files):
        """Edge: a deleted ROM with no bound shortcut (``shortcut_app_id`` is None) contributes no app_id."""
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        rom_files.files[file_a] = b"\x00" * 100
        rom_files.files[file_b] = b"\x00" * 100
        with uow:
            uow.roms.save(_make_rom(1))  # bound → 1001
            uow.roms.save(_make_rom(2, bound=False))  # unbound → no app_id
            uow.rom_installs.save(_make_install(1, file_path=file_a))
            uow.rom_installs.save(_make_install(2, file_path=file_b))

        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert result["app_ids"] == [1001]

    @pytest.mark.asyncio
    async def test_app_ids_only_for_successfully_deleted(self, service, uow, rom_files):
        """Bad path: a ROM whose file deletion raised contributes no app_id — only deleted ones are reset."""
        file_a = f"{_ROMS_BASE}/n64/game_a.z64"
        file_b = f"{_ROMS_BASE}/n64/game_b.z64"
        file_c = f"{_ROMS_BASE}/n64/game_c.z64"
        for p in (file_a, file_b, file_c):
            rom_files.files[p] = b"\x00" * 100
        rom_files.remove_file_failures.add(file_b)
        with uow:
            for rid, fp in ((1, file_a), (2, file_b), (3, file_c)):
                uow.roms.save(_make_rom(rid))
                uow.rom_installs.save(_make_install(rid, file_path=fp))

        result = await service.uninstall_all_roms()

        assert result["success"] is False
        # ROM 2's deletion raised → its app_id (1002) is absent; the deleted 1 and 3 are present.
        assert sorted(result["app_ids"]) == [1001, 1003]

    @pytest.mark.asyncio
    async def test_empty_state_returns_empty_app_ids(self, service, uow):
        """Edge: no installed ROMs → ``app_ids`` is empty."""
        _ = uow
        result = await service.uninstall_all_roms()

        assert result["app_ids"] == []


class TestDownloadQueueCleanup:
    """Eviction of the download queue on successful ROM removal."""

    @pytest.mark.asyncio
    async def test_remove_rom_evicts_queue_on_success(self, service, uow, queue_cleanup, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/zelda.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(42, file_path=rom_path))

        result = await service.remove_rom(42)
        assert result["success"] is True
        assert queue_cleanup.evicted == [42]
        assert queue_cleanup.cleared == 0

    @pytest.mark.asyncio
    async def test_remove_rom_does_not_evict_when_not_installed(self, service, queue_cleanup):
        result = await service.remove_rom(999)
        assert result["success"] is False
        assert queue_cleanup.evicted == []

    @pytest.mark.asyncio
    async def test_uninstall_all_roms_clears_queue(self, service, uow, queue_cleanup, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(1, file_path=rom_path))

        result = await service.uninstall_all_roms()
        assert result["success"] is True
        assert queue_cleanup.cleared == 1

    @pytest.mark.asyncio
    async def test_no_cleanup_dependency_is_safe(self, logger):
        """Without a ``DownloadQueueCleanup`` wired, eviction is skipped."""
        rom_files = FakeRomFileStore()
        uow = FakeUnitOfWork()
        rom_path = f"{_ROMS_BASE}/n64/g.z64"
        rom_files.files[rom_path] = b"\x00" * 100
        _seed_install(uow, _make_install(7, file_path=rom_path))

        svc = RomRemovalService(
            config=RomRemovalServiceConfig(
                logger=logger,
                loop=asyncio.get_event_loop(),
                clock=FakeClock(),
                emit=RecordingEmitter(),
                rom_file_store=rom_files,
                retrodeck_paths=FakeRetroDeckPaths(roms=_ROMS_BASE),
                download_queue_cleanup=None,
                uow_factory=FakeUnitOfWorkFactory(uow),
            ),
        )

        result = await svc.remove_rom(7)
        assert result["success"] is True

        result2 = await svc.uninstall_all_roms()
        assert result2["success"] is True


class TestBadPathRemoveRom:
    """Coverage for the ``remove_rom`` exception handler."""

    @pytest.mark.asyncio
    async def test_remove_rom_handles_filesystem_failure(self, service, uow, queue_cleanup, rom_files):
        """``remove_tree`` OSError surfaces as a failure response; the record is NOT deleted, no eviction."""
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        rom_files.remove_tree_failures.add(rom_dir)
        _seed_install(
            uow,
            _make_install(42, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"),
            platform_slug="psx",
        )

        result = await service.remove_rom(42)

        assert result["success"] is False
        assert "Failed to delete ROM files" in result["message"]
        # The install record remains because the IO helper raised before the delete UoW.
        assert uow.rom_installs.get(42) is not None
        # No queue eviction on failure.
        assert queue_cleanup.evicted == []


class TestClaimDiscipline:
    """Which claim discipline authorizes which removal."""

    def test_uninstall_claims_identity_only(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(1, file_path=rom_path))

        service._remove_rom_io(1, _installed(uow, 1))

        assert rom_files.claim_digests == [False]

    def test_bulk_uninstall_claims_identity_only(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(1, file_path=rom_path))

        service._uninstall_all_roms_io([_installed(uow, 1)])

        assert rom_files.claim_digests == [False]

    def test_a_source_a_sealed_bundle_did_not_capture_stays_content_bound(self, service, uow, rom_files):
        """The bundle exists, so the hashes still have a copy to bind this deletion to."""
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(1, file_path=rom_path))

        # A run that sealed a bundle but captured no installed ROM content.
        service.delete_rom_files(1, {})

        assert rom_files.claim_digests == [True]

    def test_a_cleanup_run_with_no_bundle_at_all_claims_identity_only(self, service, uow, rom_files):
        """Recovery off means no copy anywhere, so there is nothing for a hash to bind to."""
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(1, file_path=rom_path))

        service.delete_rom_files(1)

        assert rom_files.claim_digests == [False]

    def test_a_handed_in_claim_is_not_re_claimed(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(1, file_path=rom_path))
        claim = rom_files.claim_source(rom_path, _ROMS_BASE)
        rom_files.claim_digests.clear()

        service.delete_rom_files(1, {rom_path: claim})

        assert rom_files.claim_digests == []

    def test_self_claimed_uninstall_reads_no_file_content(self, tmp_path, logger):
        """Regression seam (#1664): a self-claimed removal hashes nothing at all."""
        import adapters.descriptor_paths as descriptor_paths

        roms = tmp_path / "roms"
        rom_dir = roms / "psx" / "Game"
        (rom_dir / "sub").mkdir(parents=True)
        (rom_dir / "disc.bin").write_bytes(b"\x00" * 4096)
        (rom_dir / "sub" / "data.bin").write_bytes(b"\x01" * 4096)
        uow = FakeUnitOfWork()
        _seed_install(
            uow,
            _make_install(1, file_path=str(rom_dir / "disc.bin"), rom_dir=str(rom_dir), system="psx"),
            platform_slug="psx",
        )
        real_service = RomRemovalService(
            config=RomRemovalServiceConfig(
                logger=logger,
                loop=asyncio.new_event_loop(),
                clock=FakeClock(),
                emit=RecordingEmitter(),
                rom_file_store=RomFileAdapter(),
                retrodeck_paths=FakeRetroDeckPaths(roms=str(roms)),
                download_queue_cleanup=None,
                uow_factory=FakeUnitOfWorkFactory(uow),
            )
        )
        original = descriptor_paths._sha256_fd
        calls = []
        descriptor_paths._sha256_fd = lambda fd, should_abort=None: calls.append(fd) or original(fd, should_abort)
        try:
            real_service._remove_rom_io(1, _installed(uow, 1))
        finally:
            descriptor_paths._sha256_fd = original

        assert calls == []
        assert not rom_dir.exists()
        assert uow.rom_installs.get(1) is None


class TestInterruptedStagingRecovery:
    """A removal interrupted between the staging rename and the last unlink (#1664)."""

    @staticmethod
    def _service(tmp_path, logger, uow, roms):
        return RomRemovalService(
            config=RomRemovalServiceConfig(
                logger=logger,
                loop=asyncio.new_event_loop(),
                clock=FakeClock(),
                emit=RecordingEmitter(),
                rom_file_store=RomFileAdapter(),
                retrodeck_paths=FakeRetroDeckPaths(roms=str(roms)),
                download_queue_cleanup=None,
                uow_factory=FakeUnitOfWorkFactory(uow),
            )
        )

    def test_a_retry_over_a_staged_away_source_recovers(self, tmp_path, logger):
        roms = tmp_path / "roms"
        rom_dir = roms / "psx" / "Game"
        rom_dir.mkdir(parents=True)
        (rom_dir / "disc.bin").write_bytes(b"\x00" * 64)
        staged = rom_dir.parent / f".Game.romm-prune-{rom_dir.stat().st_ino}"
        rom_dir.rename(staged)
        uow = FakeUnitOfWork()
        _seed_install(
            uow,
            _make_install(1, file_path=str(rom_dir / "disc.bin"), rom_dir=str(rom_dir), system="psx"),
            platform_slug="psx",
        )
        service = self._service(tmp_path, logger, uow, roms)

        result = service._delete_rom_files(_installed(uow, 1))

        assert result["success"] is True
        assert result["changed"] is True
        assert not staged.exists()
        assert not rom_dir.exists()

    def test_a_retry_drops_the_install_row_and_reports_success(self, tmp_path, logger):
        roms = tmp_path / "roms"
        rom_dir = roms / "psx" / "Game"
        rom_dir.mkdir(parents=True)
        (rom_dir / "disc.bin").write_bytes(b"\x00" * 64)
        staged = rom_dir.parent / f".Game.romm-prune-{rom_dir.stat().st_ino}"
        rom_dir.rename(staged)
        uow = FakeUnitOfWork()
        _seed_install(
            uow,
            _make_install(1, file_path=str(rom_dir / "disc.bin"), rom_dir=str(rom_dir), system="psx"),
            platform_slug="psx",
        )
        service = self._service(tmp_path, logger, uow, roms)

        service._remove_rom_io(1, _installed(uow, 1))

        assert not staged.exists()
        assert uow.rom_installs.get(1) is None

    def test_a_bundle_backed_run_never_adopts_staging_debris(self, tmp_path, logger):
        """Its authority came from a seal that a partially consumed source no longer matches."""
        roms = tmp_path / "roms"
        rom_dir = roms / "psx" / "Game"
        rom_dir.mkdir(parents=True)
        (rom_dir / "disc.bin").write_bytes(b"\x00" * 64)
        staged = rom_dir.parent / f".Game.romm-prune-{rom_dir.stat().st_ino}"
        rom_dir.rename(staged)
        uow = FakeUnitOfWork()
        _seed_install(
            uow,
            _make_install(1, file_path=str(rom_dir / "disc.bin"), rom_dir=str(rom_dir), system="psx"),
            platform_slug="psx",
        )
        service = self._service(tmp_path, logger, uow, roms)

        # A run that sealed a bundle but captured no installed ROM content.
        result = service.delete_rom_files(1, {})

        assert result["success"] is True
        assert result["changed"] is False
        assert staged.is_dir()

    def test_a_cleanup_run_with_no_bundle_adopts_debris_like_an_uninstall(self, tmp_path, logger):
        """Recovery off self-seals its claim, so the same re-seal authorizes finishing the removal."""
        roms = tmp_path / "roms"
        rom_dir = roms / "psx" / "Game"
        rom_dir.mkdir(parents=True)
        (rom_dir / "disc.bin").write_bytes(b"\x00" * 64)
        staged = rom_dir.parent / f".Game.romm-prune-{rom_dir.stat().st_ino}"
        rom_dir.rename(staged)
        uow = FakeUnitOfWork()
        _seed_install(
            uow,
            _make_install(1, file_path=str(rom_dir / "disc.bin"), rom_dir=str(rom_dir), system="psx"),
            platform_slug="psx",
        )
        service = self._service(tmp_path, logger, uow, roms)

        result = service.delete_rom_files(1)

        assert result["success"] is True
        assert result["changed"] is True
        assert not staged.exists()


class TestBulkAndSingleExclusion:
    """A bulk uninstall owns every tree, so it and a single removal exclude each other (#1664)."""

    @pytest.mark.asyncio
    async def test_a_bulk_run_is_refused_while_a_single_removal_is_in_flight(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(42, file_path=rom_path))
        bulk: dict[str, object] = {}
        entered: list[bool] = []

        original = service._delete_rom_files

        def run_a_bulk_uninstall_mid_removal(*args, **kwargs):
            # The sentinel is set *before* the nested call, not after it: were
            # the guard to regress, the bulk run would re-enter this hook while
            # `bulk` was still empty and recurse without bound. A lost guard has
            # to fail the assertion below, not hang the suite.
            if not entered:
                entered.append(True)
                bulk.update(asyncio.run_coroutine_threadsafe(service.uninstall_all_roms(), service._loop).result())
            return original(*args, **kwargs)

        service._delete_rom_files = run_a_bulk_uninstall_mid_removal
        result = await service.remove_rom(42)

        assert result["success"] is True
        assert bulk == {
            "success": False,
            "reason": "in_progress",
            "message": "A ROM is already being uninstalled",
        }
        # No removal payload: that absence is the frontend's refusal discriminant.
        assert "app_ids" not in bulk

    @pytest.mark.asyncio
    async def test_a_single_removal_is_refused_while_a_bulk_run_holds_that_rom(self, service, uow, rom_files):
        """The bulk run claims each ROM it will remove, so the per-ROM guard is what refuses."""
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(42, file_path=rom_path))
        single: dict[str, object] = {}
        entered: list[bool] = []

        original = service._delete_rom_files

        def press_uninstall_mid_bulk(*args, **kwargs):
            # Sentinel set before the nested call — see the sibling test.
            if not entered:
                entered.append(True)
                single.update(asyncio.run_coroutine_threadsafe(service.remove_rom(42), service._loop).result())
            return original(*args, **kwargs)

        service._delete_rom_files = press_uninstall_mid_bulk
        result = await service.uninstall_all_roms()

        assert result["success"] is True
        assert single == {
            "success": False,
            "reason": "in_progress",
            "message": "This ROM is already being uninstalled",
        }

    @pytest.mark.asyncio
    async def test_both_run_again_once_the_other_has_finished(self, service, uow, rom_files):
        """Edge: the guards are released, so neither entry point stays locked out."""
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(42, file_path=rom_path))

        first = await service.remove_rom(42)
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(42, file_path=rom_path))
        second = await service.uninstall_all_roms()

        assert first["success"] is True
        assert second["success"] is True
        assert second["removed_count"] == 1


class TestConcurrentUninstall:
    """The per-ROM in-flight guard (#1664)."""

    @pytest.mark.asyncio
    async def test_a_second_press_for_the_same_rom_is_refused(self, service, uow, rom_files):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(42, file_path=rom_path))
        second: dict[str, object] = {}

        original = service._delete_rom_files

        def remove_while_a_second_press_arrives(*args, **kwargs):
            second.update(asyncio.run_coroutine_threadsafe(service.remove_rom(42), service._loop).result())
            return original(*args, **kwargs)

        service._delete_rom_files = remove_while_a_second_press_arrives
        result = await service.remove_rom(42)

        assert result["success"] is True
        assert second == {
            "success": False,
            "reason": "in_progress",
            "message": "This ROM is already being uninstalled",
        }
        assert rom_path not in rom_files.files

    @pytest.mark.asyncio
    async def test_a_later_press_for_the_same_rom_is_accepted_again(self, service, uow, rom_files):
        """Edge: the guard is released, so a retry after a failure is not locked out."""
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        rom_files.remove_tree_failures.add(rom_dir)
        _seed_install(
            uow,
            _make_install(42, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"),
            platform_slug="psx",
        )

        first = await service.remove_rom(42)
        rom_files.remove_tree_failures.clear()
        second = await service.remove_rom(42)

        assert first["success"] is False
        assert second["success"] is True

    @pytest.mark.asyncio
    async def test_a_different_rom_is_not_blocked(self, service, uow, rom_files):
        path_a = f"{_ROMS_BASE}/n64/a.z64"
        path_b = f"{_ROMS_BASE}/n64/b.z64"
        rom_files.files[path_a] = b"a"
        rom_files.files[path_b] = b"b"
        _seed_install(uow, _make_install(1, file_path=path_a))
        _seed_install(uow, _make_install(2, file_path=path_b))
        other: dict[str, object] = {}

        original = service._delete_rom_files

        def remove_while_another_rom_is_pressed(*args, **kwargs):
            if not other:
                other.update(asyncio.run_coroutine_threadsafe(service.remove_rom(2), service._loop).result())
            return original(*args, **kwargs)

        service._delete_rom_files = remove_while_another_rom_is_pressed
        result = await service.remove_rom(1)

        assert result["success"] is True
        assert other["success"] is True


class TestRemovalProgressFrames:
    """``uninstall_progress`` visibility for a removal long enough to look dead (#1664)."""

    @pytest.mark.asyncio
    async def test_a_multi_file_removal_emits_a_terminal_frame(self, service, uow, rom_files, emitter):
        rom_dir = f"{_ROMS_BASE}/psx/FF7"
        rom_files.files[f"{rom_dir}/disc1.bin"] = b"\x00" * 100
        rom_files.files[f"{rom_dir}/disc2.bin"] = b"\x00" * 100
        _seed_install(
            uow,
            _make_install(42, file_path=f"{rom_dir}/FF7.m3u", rom_dir=rom_dir, system="psx"),
            platform_slug="psx",
        )

        await service.remove_rom(42)
        await asyncio.sleep(0)

        assert emitter.payloads("uninstall_progress")[-1] == {
            "rom_id": 42,
            "files_removed": 2,
            "files_total": 2,
        }

    @pytest.mark.asyncio
    async def test_a_single_file_removal_emits_nothing(self, service, uow, rom_files, emitter):
        rom_path = f"{_ROMS_BASE}/n64/game.z64"
        rom_files.files[rom_path] = b"rom"
        _seed_install(uow, _make_install(42, file_path=rom_path))

        await service.remove_rom(42)
        await asyncio.sleep(0)

        assert emitter.payloads("uninstall_progress") == []
