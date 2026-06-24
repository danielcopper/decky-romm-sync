"""Tests for LaunchGateService."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, cast

import pytest

from services.launch_gate import (
    LaunchGateService,
    LaunchGateServiceConfig,
    LaunchVerdict,
)

if TYPE_CHECKING:
    from models.state import InstalledRomEntry

    from services.protocols.files import SaveFileStore


def _installed_rom(rom_id: int) -> InstalledRomEntry:
    """Build a sparse InstalledRomEntry — this test only checks truthiness / rom_id."""
    return cast("InstalledRomEntry", {"rom_id": rom_id})


class FakeRomLookup:
    """In-memory ``LaunchGateRomLookup`` for tests."""

    def __init__(self, *, mapping: dict[int, dict[str, Any]] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {}
        self.calls: list[int] = []

    def get_rom_by_steam_app_id(self, app_id: int) -> dict[str, Any] | None:
        self.calls.append(app_id)
        return self.mapping.get(app_id)


class FakeInstalledChecker:
    """In-memory ``LaunchGateInstalledChecker`` for tests."""

    def __init__(self, *, installed: dict[int, InstalledRomEntry] | None = None) -> None:
        self.installed = installed if installed is not None else {}
        self.calls: list[int] = []

    def get_installed_rom(self, rom_id: int) -> InstalledRomEntry | None:
        self.calls.append(rom_id)
        return self.installed.get(rom_id)


class FakeSaveStatusReader:
    """In-memory ``LaunchGateSaveStatusReader`` for tests."""

    def __init__(
        self,
        *,
        payload: dict[str, Any] | None = None,
        side_effect: BaseException | None = None,
        tracked_rom_ids: set[int] | None = None,
        save_sync_enabled: bool = True,
    ) -> None:
        self.payload: dict[str, Any] = payload if payload is not None else {"conflicts": []}
        self.side_effect = side_effect
        self.tracked_rom_ids: set[int] = tracked_rom_ids if tracked_rom_ids is not None else set()
        self.save_sync_enabled = save_sync_enabled
        self.calls: list[int] = []
        self.tracked_calls: list[int] = []

    def is_save_sync_enabled(self) -> bool:
        return self.save_sync_enabled

    async def get_save_status(self, rom_id: int) -> dict[str, Any]:
        self.calls.append(rom_id)
        if self.side_effect is not None:
            raise self.side_effect
        return self.payload

    def has_tracked_save(self, rom_id: int) -> bool:
        self.tracked_calls.append(rom_id)
        return rom_id in self.tracked_rom_ids


class FakeDriftReader:
    """In-memory ``LaunchGateDriftReader`` for tests.

    ``files`` maps rom_id → the ``find_local_save_files`` list; ``baselines``
    maps rom_id → the ``last_sync_hashes`` map. ``find_raises`` /
    ``baselines_raises`` arm the internal-error path.
    """

    def __init__(
        self,
        *,
        files: dict[int, list[dict[str, str]]] | None = None,
        baselines: dict[int, dict[str, str | None]] | None = None,
        find_raises: BaseException | None = None,
        baselines_raises: BaseException | None = None,
    ) -> None:
        self.files = files if files is not None else {}
        self.baselines = baselines if baselines is not None else {}
        self.find_raises = find_raises
        self.baselines_raises = baselines_raises

    def find_local_save_files(self, rom_id: int) -> list[dict[str, str]]:
        if self.find_raises is not None:
            raise self.find_raises
        return self.files.get(rom_id, [])

    def last_sync_hashes(self, rom_id: int) -> dict[str, str | None]:
        if self.baselines_raises is not None:
            raise self.baselines_raises
        return self.baselines.get(rom_id, {})


class FakeSaveFileStore:
    """Minimal ``SaveFileStore`` stub — only ``checksum_md5`` is exercised here.

    ``hashes`` maps path → its content MD5; ``checksum_raises`` arms a
    mid-hash failure (e.g. the file vanished between enumeration and hashing).
    """

    def __init__(
        self,
        *,
        hashes: dict[str, str] | None = None,
        checksum_raises: BaseException | None = None,
    ) -> None:
        self.hashes = hashes if hashes is not None else {}
        self.checksum_raises = checksum_raises
        self.checksum_calls: list[str] = []

    def checksum_md5(self, path: str) -> str:
        self.checksum_calls.append(path)
        if self.checksum_raises is not None:
            raise self.checksum_raises
        return self.hashes[path]


@pytest.fixture
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_launch_gate")


def _make_service(
    *,
    rom_lookup: FakeRomLookup,
    installed_checker: FakeInstalledChecker,
    save_status_reader: FakeSaveStatusReader,
    logger: logging.Logger,
    loop: asyncio.AbstractEventLoop | None = None,
    drift_reader: FakeDriftReader | None = None,
    save_file_store: FakeSaveFileStore | None = None,
) -> LaunchGateService:
    return LaunchGateService(
        config=LaunchGateServiceConfig(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            drift_reader=drift_reader if drift_reader is not None else FakeDriftReader(),
            save_file_store=cast(
                "SaveFileStore", save_file_store if save_file_store is not None else FakeSaveFileStore()
            ),
            loop=loop if loop is not None else asyncio.get_event_loop(),
            logger=logger,
        ),
    )


class TestEvaluateAllow:
    def test_not_a_romm_game_allows_silently(self, event_loop, logger):
        """rom_lookup returns None → allow, no reason, no toast strings."""
        rom_lookup = FakeRomLookup()
        installed_checker = FakeInstalledChecker()
        save_status_reader = FakeSaveStatusReader()
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(123_456))

        assert verdict == LaunchVerdict(action="allow")
        assert rom_lookup.calls == [123_456]
        # Downstream deps must not be touched once rom_lookup signals "not a RomM game".
        assert installed_checker.calls == []
        assert save_status_reader.calls == []

    def test_installed_with_no_conflicts_allows(self, event_loop, logger):
        """ROM installed, save_status returns no conflicts → allow."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99, "name": "Game"}})
        installed_checker = FakeInstalledChecker(installed={99: _installed_rom(99)})
        save_status_reader = FakeSaveStatusReader(payload={"conflicts": []})
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict == LaunchVerdict(action="allow")
        assert rom_lookup.calls == [42]
        assert installed_checker.calls == [99]
        assert save_status_reader.calls == [99]

    def test_save_status_failure_no_tracked_saves_allows(self, event_loop, logger):
        """ROM installed, save_status raises, no tracked saves → allow (nothing to corrupt)."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={99: _installed_rom(99)})
        save_status_reader = FakeSaveStatusReader(
            side_effect=RuntimeError("boom"),
            tracked_rom_ids=set(),
        )
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict == LaunchVerdict(action="allow")
        assert save_status_reader.calls == [99]
        assert save_status_reader.tracked_calls == [99]


class TestEvaluateBlock:
    def test_not_installed_blocks_with_download_toast(self, event_loop, logger):
        """ROM is a RomM game but not installed → block, not_installed."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99, "name": "Game"}})
        installed_checker = FakeInstalledChecker(installed={})  # nothing installed
        save_status_reader = FakeSaveStatusReader()
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict.action == "block"
        assert verdict.reason == "not_installed"
        assert verdict.toast_title == "RomM Sync"
        assert verdict.toast_body == "ROM not downloaded. Open the game page to download it first."
        # Save-status reader must not be consulted when the ROM is not installed.
        assert save_status_reader.calls == []

    def test_save_conflict_blocks_with_resolve_toast(self, event_loop, logger):
        """ROM installed, conflicts non-empty → block, save_conflict."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={99: _installed_rom(99)})
        save_status_reader = FakeSaveStatusReader(
            payload={
                "conflicts": [
                    {
                        "type": "sync_conflict",
                        "rom_id": 99,
                        "filename": "game.srm",
                        "server_save_id": 7,
                    }
                ]
            },
        )
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict.action == "block"
        assert verdict.reason == "save_conflict"
        assert verdict.toast_title == "RomM Save Sync"
        assert verdict.toast_body == "Save conflict detected — open game page to resolve before playing"


class TestEvaluateWarn:
    def test_save_status_failure_with_tracked_saves_warns(self, event_loop, logger, caplog):
        """ROM installed, save_status raises OSError, tracked saves present → warn."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={99: _installed_rom(99)})
        save_status_reader = FakeSaveStatusReader(
            side_effect=OSError("network down"),
            tracked_rom_ids={99},
        )
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        with caplog.at_level("WARNING", logger="test_launch_gate"):
            verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict.action == "warn"
        assert verdict.reason == "save_status_failed"
        assert verdict.toast_title == "RomM Save Sync"
        assert verdict.toast_body == "Save-status check failed — retry?"
        assert save_status_reader.calls == [99]
        assert save_status_reader.tracked_calls == [99]
        # Bumped from DEBUG to WARNING — verify the level and that the
        # exception detail is included.
        warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warning_records) == 1
        assert "rom_id=99" in warning_records[0].getMessage()
        assert "network down" in warning_records[0].getMessage()

    def test_save_status_failure_warn_when_only_slots_tracked(self, event_loop, logger):
        """``has_tracked_save`` returning True (e.g. slots-only entry) → warn."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={99: _installed_rom(99)})
        save_status_reader = FakeSaveStatusReader(
            side_effect=RuntimeError("executor crash"),
            tracked_rom_ids={99},
        )
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict.action == "warn"
        assert verdict.reason == "save_status_failed"


class TestEvaluateEdgeCases:
    def test_save_status_missing_conflicts_key_allows(self, event_loop, logger):
        """Save status without ``conflicts`` key is treated as no conflicts."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={99: _installed_rom(99)})
        save_status_reader = FakeSaveStatusReader(payload={"rom_id": 99, "files": []})
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict == LaunchVerdict(action="allow")

    def test_save_status_conflicts_none_allows(self, event_loop, logger):
        """``conflicts`` explicitly None is treated as no conflicts."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={99: _installed_rom(99)})
        save_status_reader = FakeSaveStatusReader(payload={"conflicts": None})
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict == LaunchVerdict(action="allow")

    def test_save_status_empty_dict_allows(self, event_loop, logger):
        """An empty save-status dict (falsy save_status branch) is treated as no conflicts."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={99: _installed_rom(99)})
        save_status_reader = FakeSaveStatusReader(payload={})
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict == LaunchVerdict(action="allow")


class TestEvaluateSaveSyncDisabled:
    def test_disabled_installed_allows_and_skips_status_round_trip(self, event_loop, logger):
        """Save-sync off + installed → allow, even with a server-side conflict, and ``get_save_status`` is never called.

        Regression for #1056: a stale conflict (another device moved the save
        while sync was disabled) must not block the launch, and the gate must
        not perform a RomM round-trip on every direct launch while the feature
        is off.
        """
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99, "name": "Game"}})
        installed_checker = FakeInstalledChecker(installed={99: _installed_rom(99)})
        save_status_reader = FakeSaveStatusReader(
            payload={"conflicts": [{"type": "sync_conflict", "rom_id": 99, "filename": "game.srm"}]},
            save_sync_enabled=False,
        )
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict == LaunchVerdict(action="allow")
        assert installed_checker.calls == [99]
        # The conflict round-trip must be skipped entirely while save-sync is off.
        assert save_status_reader.calls == []
        assert save_status_reader.tracked_calls == []

    def test_disabled_still_blocks_not_installed(self, event_loop, logger):
        """The save-sync-disabled allow is gated behind the not-installed check — uninstalled still blocks."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={})  # not installed
        save_status_reader = FakeSaveStatusReader(save_sync_enabled=False)
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict.action == "block"
        assert verdict.reason == "not_installed"
        assert save_status_reader.calls == []


class TestCheckLocalDrift:
    def _drift_service(
        self,
        *,
        event_loop: asyncio.AbstractEventLoop,
        logger: logging.Logger,
        drift_reader: FakeDriftReader,
        save_file_store: FakeSaveFileStore | None = None,
    ) -> LaunchGateService:
        return _make_service(
            rom_lookup=FakeRomLookup(),
            installed_checker=FakeInstalledChecker(),
            save_status_reader=FakeSaveStatusReader(),
            logger=logger,
            loop=event_loop,
            drift_reader=drift_reader,
            save_file_store=save_file_store if save_file_store is not None else FakeSaveFileStore(),
        )

    def test_not_installed_no_local_files_not_drifted(self, event_loop, logger):
        """No local save files (not installed / nothing on disk) → drifted False, never hashes."""
        drift_reader = FakeDriftReader(files={})
        save_file_store = FakeSaveFileStore()
        service = self._drift_service(
            event_loop=event_loop, logger=logger, drift_reader=drift_reader, save_file_store=save_file_store
        )

        result = event_loop.run_until_complete(service.check_local_drift(99))

        assert result == {"drifted": False, "rom_id": 99}
        assert save_file_store.checksum_calls == []

    def test_single_file_hash_matches_not_drifted(self, event_loop, logger):
        """One local file whose current hash equals its baseline → drifted False."""
        drift_reader = FakeDriftReader(
            files={42: [{"path": "/saves/game.srm", "filename": "game.srm"}]},
            baselines={42: {"game.srm": "hash-A"}},
        )
        save_file_store = FakeSaveFileStore(hashes={"/saves/game.srm": "hash-A"})
        service = self._drift_service(
            event_loop=event_loop, logger=logger, drift_reader=drift_reader, save_file_store=save_file_store
        )

        result = event_loop.run_until_complete(service.check_local_drift(42))

        assert result == {"drifted": False, "rom_id": 42}
        assert save_file_store.checksum_calls == ["/saves/game.srm"]

    def test_single_file_hash_mismatch_drifted(self, event_loop, logger):
        """One local file whose current hash differs from its baseline → drifted True."""
        drift_reader = FakeDriftReader(
            files={42: [{"path": "/saves/game.srm", "filename": "game.srm"}]},
            baselines={42: {"game.srm": "hash-A"}},
        )
        save_file_store = FakeSaveFileStore(hashes={"/saves/game.srm": "hash-B"})
        service = self._drift_service(
            event_loop=event_loop, logger=logger, drift_reader=drift_reader, save_file_store=save_file_store
        )

        result = event_loop.run_until_complete(service.check_local_drift(42))

        assert result == {"drifted": True, "rom_id": 42}

    def test_no_baseline_is_not_drift(self, event_loop, logger):
        """A present file whose baseline is None (never synced) → not drift, never hashes it."""
        drift_reader = FakeDriftReader(
            files={42: [{"path": "/saves/game.srm", "filename": "game.srm"}]},
            baselines={42: {"game.srm": None}},
        )
        save_file_store = FakeSaveFileStore(hashes={"/saves/game.srm": "hash-A"})
        service = self._drift_service(
            event_loop=event_loop, logger=logger, drift_reader=drift_reader, save_file_store=save_file_store
        )

        result = event_loop.run_until_complete(service.check_local_drift(42))

        assert result == {"drifted": False, "rom_id": 42}
        # No baseline → nothing to compare, so the file is never hashed.
        assert save_file_store.checksum_calls == []

    def test_missing_baseline_key_is_not_drift(self, event_loop, logger):
        """A present file with no baseline entry at all → not drift (baselines.get → None)."""
        drift_reader = FakeDriftReader(
            files={42: [{"path": "/saves/game.srm", "filename": "game.srm"}]},
            baselines={42: {}},
        )
        save_file_store = FakeSaveFileStore(hashes={"/saves/game.srm": "hash-A"})
        service = self._drift_service(
            event_loop=event_loop, logger=logger, drift_reader=drift_reader, save_file_store=save_file_store
        )

        result = event_loop.run_until_complete(service.check_local_drift(42))

        assert result == {"drifted": False, "rom_id": 42}
        assert save_file_store.checksum_calls == []

    def test_multi_file_one_drifted_is_drifted(self, event_loop, logger):
        """Multiple component files, one drifted → drifted True."""
        drift_reader = FakeDriftReader(
            files={
                7: [
                    {"path": "/saves/game.bkr", "filename": "game.bkr"},
                    {"path": "/saves/game.bcr", "filename": "game.bcr"},
                ]
            },
            baselines={7: {"game.bkr": "bkr-base", "game.bcr": "bcr-base"}},
        )
        save_file_store = FakeSaveFileStore(
            hashes={"/saves/game.bkr": "bkr-base", "/saves/game.bcr": "bcr-CHANGED"},
        )
        service = self._drift_service(
            event_loop=event_loop, logger=logger, drift_reader=drift_reader, save_file_store=save_file_store
        )

        result = event_loop.run_until_complete(service.check_local_drift(7))

        assert result == {"drifted": True, "rom_id": 7}

    def test_internal_error_during_enumeration_is_not_drifted(self, event_loop, logger, caplog):
        """``find_local_save_files`` raising → drifted False (no raise), logged."""
        drift_reader = FakeDriftReader(find_raises=RuntimeError("uow boom"))
        service = self._drift_service(event_loop=event_loop, logger=logger, drift_reader=drift_reader)

        with caplog.at_level("WARNING", logger="test_launch_gate"):
            result = event_loop.run_until_complete(service.check_local_drift(42))

        assert result == {"drifted": False, "rom_id": 42}
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert "rom_id=42" in warnings[0].getMessage()

    def test_internal_error_during_hash_is_not_drifted(self, event_loop, logger):
        """``checksum_md5`` raising mid-hash → drifted False (no raise)."""
        drift_reader = FakeDriftReader(
            files={42: [{"path": "/saves/game.srm", "filename": "game.srm"}]},
            baselines={42: {"game.srm": "hash-A"}},
        )
        save_file_store = FakeSaveFileStore(checksum_raises=OSError("file vanished"))
        service = self._drift_service(
            event_loop=event_loop, logger=logger, drift_reader=drift_reader, save_file_store=save_file_store
        )

        result = event_loop.run_until_complete(service.check_local_drift(42))

        assert result == {"drifted": False, "rom_id": 42}
