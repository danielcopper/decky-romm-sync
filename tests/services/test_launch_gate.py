"""Tests for LaunchGateService."""

from __future__ import annotations

import asyncio
import logging

import pytest

from services.launch_gate import (
    LaunchGateService,
    LaunchGateServiceConfig,
    LaunchVerdict,
)


class FakeRomLookup:
    """In-memory ``LaunchGateRomLookup`` for tests."""

    def __init__(self, *, mapping: dict[int, dict] | None = None) -> None:
        self.mapping = mapping if mapping is not None else {}
        self.calls: list[int] = []

    def get_rom_by_steam_app_id(self, app_id: int) -> dict | None:
        self.calls.append(app_id)
        return self.mapping.get(app_id)


class FakeInstalledChecker:
    """In-memory ``LaunchGateInstalledChecker`` for tests."""

    def __init__(self, *, installed: dict[int, dict] | None = None) -> None:
        self.installed = installed if installed is not None else {}
        self.calls: list[int] = []

    def get_installed_rom(self, rom_id: int) -> dict | None:
        self.calls.append(rom_id)
        return self.installed.get(rom_id)


class FakeSaveStatusReader:
    """In-memory ``LaunchGateSaveStatusReader`` for tests."""

    def __init__(
        self,
        *,
        payload: dict | None = None,
        side_effect: BaseException | None = None,
    ) -> None:
        self.payload: dict = payload if payload is not None else {"conflicts": []}
        self.side_effect = side_effect
        self.calls: list[int] = []

    async def get_save_status(self, rom_id: int) -> dict:
        self.calls.append(rom_id)
        if self.side_effect is not None:
            raise self.side_effect
        return self.payload


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
) -> LaunchGateService:
    return LaunchGateService(
        config=LaunchGateServiceConfig(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
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
        installed_checker = FakeInstalledChecker(installed={99: {"rom_id": 99}})
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

    def test_save_status_failure_does_not_block(self, event_loop, logger):
        """ROM installed, save_status raises → allow (non-critical fallback)."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={99: {"rom_id": 99}})
        save_status_reader = FakeSaveStatusReader(side_effect=RuntimeError("boom"))
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict == LaunchVerdict(action="allow")
        assert save_status_reader.calls == [99]


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
        installed_checker = FakeInstalledChecker(installed={99: {"rom_id": 99}})
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


class TestEvaluateEdgeCases:
    def test_save_status_missing_conflicts_key_allows(self, event_loop, logger):
        """Save status without ``conflicts`` key is treated as no conflicts."""
        rom_lookup = FakeRomLookup(mapping={42: {"rom_id": 99}})
        installed_checker = FakeInstalledChecker(installed={99: {"rom_id": 99}})
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
        installed_checker = FakeInstalledChecker(installed={99: {"rom_id": 99}})
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
        installed_checker = FakeInstalledChecker(installed={99: {"rom_id": 99}})
        save_status_reader = FakeSaveStatusReader(payload={})
        service = _make_service(
            rom_lookup=rom_lookup,
            installed_checker=installed_checker,
            save_status_reader=save_status_reader,
            logger=logger,
        )

        verdict = event_loop.run_until_complete(service.evaluate(42))

        assert verdict == LaunchVerdict(action="allow")
