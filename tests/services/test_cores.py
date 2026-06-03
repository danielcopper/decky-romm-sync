"""Tests for CoreService."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest
from fakes.fake_core_info_provider import FakeCoreInfoProvider
from fakes.fake_retrodeck_paths import FakeRetroDeckPaths

from services.cores import CoreService, CoreServiceConfig


class FakeGamelistEditor:
    """In-memory ``GamelistXmlEditor`` for tests."""

    def __init__(self) -> None:
        self.system_calls: list[tuple[str, str, str | None]] = []
        self.game_calls: list[tuple[str, str, str, str | None]] = []
        self.system_side_effect: BaseException | None = None
        self.game_side_effect: BaseException | None = None

    def set_system_override(self, retrodeck_home: str, system_name: str, core_label: str | None) -> bool:
        if self.system_side_effect is not None:
            raise self.system_side_effect
        self.system_calls.append((retrodeck_home, system_name, core_label))
        return True

    def set_game_override(
        self,
        retrodeck_home: str,
        system_name: str,
        rom_path: str,
        core_label: str | None,
    ) -> bool:
        if self.game_side_effect is not None:
            raise self.game_side_effect
        self.game_calls.append((retrodeck_home, system_name, rom_path, core_label))
        return True


class FakeBiosChecker:
    """In-memory ``BiosChecker`` for tests (only implements the async entry CoreService uses)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.payload: dict[str, Any] = {"needs_bios": False}
        self.side_effect: BaseException | None = None

    async def check_platform_bios(self, platform_slug: str, rom_filename: str | None = None) -> dict[str, Any]:
        if self.side_effect is not None:
            raise self.side_effect
        self.calls.append((platform_slug, rom_filename))
        return self.payload


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
        available_cores=[{"core_so": "snes9x_libretro", "label": "Snes9x"}],
    )


@pytest.fixture
def gamelist_editor() -> FakeGamelistEditor:
    return FakeGamelistEditor()


@pytest.fixture
def bios_checker() -> FakeBiosChecker:
    return FakeBiosChecker()


@pytest.fixture
def retrodeck_paths() -> FakeRetroDeckPaths:
    return FakeRetroDeckPaths(home="/home/deck/retrodeck")


@pytest.fixture
def service(event_loop, logger, core_info, gamelist_editor, bios_checker, retrodeck_paths) -> CoreService:
    return CoreService(
        config=CoreServiceConfig(
            loop=event_loop,
            logger=logger,
            core_info=core_info,
            gamelist_editor=gamelist_editor,
            retrodeck_paths=retrodeck_paths,
            bios_checker=bios_checker,
        ),
    )


# ── get_available_cores ────────────────────────────────────────────────


class TestGetAvailableCores:
    def test_happy_path(self, event_loop, service, core_info):
        result = event_loop.run_until_complete(service.get_available_cores("snes"))
        assert result == {
            "cores": core_info.available_cores,
            "active_core": "snes9x_libretro",
            "active_core_label": "Snes9x",
        }

    def test_no_active_core(self, event_loop, service, core_info):
        core_info.active_core = (None, None)
        result = event_loop.run_until_complete(service.get_available_cores("snes"))
        assert result["active_core"] is None
        assert result["active_core_label"] is None

    def test_empty_cores_list(self, event_loop, service, core_info):
        core_info.available_cores = []
        result = event_loop.run_until_complete(service.get_available_cores("snes"))
        assert result["cores"] == []


# ── set_system_core ────────────────────────────────────────────────────


class TestSetSystemCore:
    def test_happy_path(self, event_loop, service, core_info, gamelist_editor, bios_checker):
        bios_checker.payload = {"needs_bios": True, "files": []}
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result == {"success": True, "bios_status": {"needs_bios": True, "files": []}}
        assert gamelist_editor.system_calls == [("/home/deck/retrodeck", "snes", "Snes9x")]
        assert core_info.reset_cache_count == 1
        assert bios_checker.calls == [("snes", None)]

    def test_empty_core_label_clears_override(self, event_loop, service, gamelist_editor):
        result = event_loop.run_until_complete(service.set_system_core("snes", ""))
        assert result["success"] is True
        # Editor sees None when core_label is the empty string.
        assert gamelist_editor.system_calls == [("/home/deck/retrodeck", "snes", None)]

    def test_no_retrodeck_home(
        self,
        event_loop,
        service,
        retrodeck_paths,
        gamelist_editor,
        bios_checker,
        core_info,
    ):
        retrodeck_paths.home = ""
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result == {"success": False, "message": "RetroDECK home not found"}
        assert gamelist_editor.system_calls == []
        assert bios_checker.calls == []
        assert core_info.reset_cache_count == 0

    def test_editor_raises_returns_error(self, event_loop, service, gamelist_editor, bios_checker):
        gamelist_editor.system_side_effect = RuntimeError("xml write failed")
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result["success"] is False
        assert "xml write failed" in result["message"]
        # BIOS checker must not be invoked after a failed write.
        assert bios_checker.calls == []

    def test_bios_checker_raises_returns_error(self, event_loop, service, gamelist_editor, bios_checker):
        bios_checker.side_effect = RuntimeError("bios probe failed")
        result = event_loop.run_until_complete(service.set_system_core("snes", "Snes9x"))
        assert result["success"] is False
        assert "bios probe failed" in result["message"]
        # The write itself still happened.
        assert gamelist_editor.system_calls == [("/home/deck/retrodeck", "snes", "Snes9x")]


# ── set_game_core ──────────────────────────────────────────────────────


class TestSetGameCore:
    def test_happy_path(self, event_loop, service, core_info, gamelist_editor, bios_checker):
        result = event_loop.run_until_complete(service.set_game_core("n64", "n64/zelda.z64", "Mupen64Plus"))
        assert result == {"success": True, "bios_status": {"needs_bios": False}}
        assert gamelist_editor.game_calls == [
            ("/home/deck/retrodeck", "n64", "n64/zelda.z64", "Mupen64Plus"),
        ]
        assert core_info.reset_cache_count == 1
        assert bios_checker.calls == [("n64", "n64/zelda.z64")]

    def test_rom_path_with_leading_dotslash(self, event_loop, service, bios_checker):
        result = event_loop.run_until_complete(
            service.set_game_core("n64", "./n64/zelda.z64", "Mupen64Plus"),
        )
        assert result["success"] is True
        assert bios_checker.calls == [("n64", "n64/zelda.z64")]

    def test_empty_rom_path_yields_none_filename(self, event_loop, service, gamelist_editor, bios_checker):
        result = event_loop.run_until_complete(service.set_game_core("n64", "", "Mupen64Plus"))
        assert result["success"] is True
        assert bios_checker.calls == [("n64", None)]
        # The editor still receives the empty rom_path verbatim — the
        # write-side fallback is "set None core_label", not "skip write".
        assert gamelist_editor.game_calls == [("/home/deck/retrodeck", "n64", "", "Mupen64Plus")]

    def test_empty_core_label_clears_override(self, event_loop, service, gamelist_editor):
        result = event_loop.run_until_complete(service.set_game_core("n64", "n64/zelda.z64", ""))
        assert result["success"] is True
        assert gamelist_editor.game_calls == [
            ("/home/deck/retrodeck", "n64", "n64/zelda.z64", None),
        ]

    def test_no_retrodeck_home(
        self,
        event_loop,
        service,
        retrodeck_paths,
        gamelist_editor,
        bios_checker,
        core_info,
    ):
        retrodeck_paths.home = ""
        result = event_loop.run_until_complete(
            service.set_game_core("n64", "n64/zelda.z64", "Mupen64Plus"),
        )
        assert result == {"success": False, "message": "RetroDECK home not found"}
        assert gamelist_editor.game_calls == []
        assert bios_checker.calls == []
        assert core_info.reset_cache_count == 0

    def test_editor_raises_returns_error(self, event_loop, service, gamelist_editor, bios_checker):
        gamelist_editor.game_side_effect = RuntimeError("xml write failed")
        result = event_loop.run_until_complete(
            service.set_game_core("n64", "n64/zelda.z64", "Mupen64Plus"),
        )
        assert result["success"] is False
        assert "xml write failed" in result["message"]
        assert bios_checker.calls == []

    def test_bios_checker_raises_returns_error(self, event_loop, service, gamelist_editor, bios_checker):
        bios_checker.side_effect = RuntimeError("bios probe failed")
        result = event_loop.run_until_complete(
            service.set_game_core("n64", "n64/zelda.z64", "Mupen64Plus"),
        )
        assert result["success"] is False
        assert "bios probe failed" in result["message"]
        assert gamelist_editor.game_calls == [
            ("/home/deck/retrodeck", "n64", "n64/zelda.z64", "Mupen64Plus"),
        ]
