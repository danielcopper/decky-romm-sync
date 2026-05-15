"""Tests for StartupHealingService."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from conftest import FakePathProbe, FakeRetroDeckPaths

from services.startup_healing import StartupHealingService, StartupHealingServiceConfig

_RETRODECK_HOME = "/run/media/deck/Emulation/retrodeck"


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_startup_healing")


@pytest.fixture
def save_state() -> MagicMock:
    return MagicMock()


def _make_state() -> dict:
    return {
        "installed_roms": {},
        "shortcut_registry": {},
    }


def _make_service(
    *,
    state: dict,
    logger: logging.Logger,
    save_state: MagicMock,
    retrodeck_home: str = _RETRODECK_HOME,
    path_probe: FakePathProbe | None = None,
) -> StartupHealingService:
    probe = path_probe if path_probe is not None else FakePathProbe(paths={retrodeck_home})
    return StartupHealingService(
        config=StartupHealingServiceConfig(
            state=state,
            logger=logger,
            save_state=save_state,
            retrodeck_paths=FakeRetroDeckPaths(home=retrodeck_home),
            path_probe=probe,
        ),
    )


class TestPruneStaleInstalledRoms:
    def test_skip_when_retrodeck_home_missing_on_disk(self, logger, save_state, caplog):
        """Guard: retrodeck home not present on disk → skip prune, log info."""
        state = _make_state()
        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/run/media/deck/Emulation/retrodeck/roms/n64/a.z64"},
        }
        # path_probe knows nothing — retrodeck home not on disk.
        service = _make_service(
            state=state,
            logger=logger,
            save_state=save_state,
            path_probe=FakePathProbe(),
        )
        with caplog.at_level(logging.INFO):
            service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        save_state.assert_not_called()
        assert any("retrodeck home unavailable" in rec.message for rec in caplog.records)

    def test_skip_when_retrodeck_home_unset(self, logger, save_state):
        """Empty retrodeck_home (first-run) → skip prune."""
        state = _make_state()
        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/somewhere/a.z64"},
        }
        service = _make_service(
            state=state,
            logger=logger,
            save_state=save_state,
            retrodeck_home="",
            path_probe=FakePathProbe(),
        )
        service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        save_state.assert_not_called()

    def test_prune_missing_file_path(self, logger, save_state):
        state = _make_state()
        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/nonexistent/game.z64"},
        }
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_installed_roms()
        assert "1" not in state["installed_roms"]
        save_state.assert_called_once()

    def test_preserve_existing_file_path(self, logger, save_state):
        state = _make_state()
        rom_file = "/run/media/deck/Emulation/retrodeck/roms/n64/game.z64"
        state["installed_roms"] = {"1": {"rom_id": 1, "file_path": rom_file}}
        probe = FakePathProbe(paths={_RETRODECK_HOME, rom_file})
        service = _make_service(state=state, logger=logger, save_state=save_state, path_probe=probe)
        service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        save_state.assert_not_called()

    def test_preserve_via_rom_dir_fallback(self, logger, save_state):
        """file_path missing but rom_dir exists → entry preserved (PSX multi-file fallback)."""
        state = _make_state()
        rom_dir = "/run/media/deck/Emulation/retrodeck/roms/psx/FF7"
        state["installed_roms"] = {
            "1": {
                "rom_id": 1,
                "file_path": f"{rom_dir}/FF7.m3u",  # file gone
                "rom_dir": rom_dir,
            },
        }
        probe = FakePathProbe(paths={_RETRODECK_HOME, rom_dir})
        service = _make_service(state=state, logger=logger, save_state=save_state, path_probe=probe)
        service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        save_state.assert_not_called()

    def test_preserve_pending_migration_entry(self, logger, save_state, caplog):
        """Entry under pending migration's previous home → preserved with info log."""
        state = _make_state()
        state["retrodeck_home_path_previous"] = "/old/retrodeck"
        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/old/retrodeck/roms/n64/zelda.z64"},
        }
        service = _make_service(state=state, logger=logger, save_state=save_state)
        with caplog.at_level(logging.INFO):
            service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        save_state.assert_not_called()
        assert any("Skipping prune" in rec.message and "/old/retrodeck" in rec.message for rec in caplog.records)

    def test_no_prune_does_not_save(self, logger, save_state):
        """When no entry is pruned, save_state is not invoked."""
        state = _make_state()
        # Empty installed_roms — nothing to prune.
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_installed_roms()
        save_state.assert_not_called()

    def test_mixed_prune_some_preserve_others(self, logger, save_state):
        state = _make_state()
        existing = "/run/media/deck/Emulation/retrodeck/roms/n64/keep.z64"
        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": existing},
            "2": {"rom_id": 2, "file_path": "/gone/dead.z64"},
        }
        probe = FakePathProbe(paths={_RETRODECK_HOME, existing})
        service = _make_service(state=state, logger=logger, save_state=save_state, path_probe=probe)
        service.prune_stale_installed_roms()
        assert "1" in state["installed_roms"]
        assert "2" not in state["installed_roms"]
        save_state.assert_called_once()

    def test_prefix_false_match_not_preserved(self, logger, save_state):
        """``pending_home="/foo"`` does NOT preserve ``/foobar/x``."""
        state = _make_state()
        state["retrodeck_home_path_previous"] = "/foo"
        state["installed_roms"] = {
            "1": {"rom_id": 1, "file_path": "/foobar/x.z64"},
        }
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_installed_roms()
        assert "1" not in state["installed_roms"]
        save_state.assert_called_once()


class TestPruneStaleRegistry:
    def test_prune_missing_app_id(self, logger, save_state):
        state = _make_state()
        state["shortcut_registry"] = {"1": {"name": "Game"}}
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_registry()
        assert "1" not in state["shortcut_registry"]
        save_state.assert_called_once()

    def test_prune_zero_app_id(self, logger, save_state):
        state = _make_state()
        state["shortcut_registry"] = {"1": {"app_id": 0, "name": "Game"}}
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_registry()
        assert "1" not in state["shortcut_registry"]

    def test_prune_string_app_id(self, logger, save_state):
        state = _make_state()
        state["shortcut_registry"] = {"1": {"app_id": "42", "name": "Game"}}
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_registry()
        assert "1" not in state["shortcut_registry"]

    def test_prune_none_app_id(self, logger, save_state):
        state = _make_state()
        state["shortcut_registry"] = {"1": {"app_id": None, "name": "Game"}}
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_registry()
        assert "1" not in state["shortcut_registry"]

    def test_preserve_valid_app_id(self, logger, save_state):
        state = _make_state()
        state["shortcut_registry"] = {"1": {"app_id": 1234567890, "name": "Game"}}
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_registry()
        assert "1" in state["shortcut_registry"]
        save_state.assert_not_called()

    def test_no_prune_does_not_save(self, logger, save_state):
        state = _make_state()
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_registry()
        save_state.assert_not_called()

    def test_mixed_prune_some_preserve_others(self, logger, save_state):
        state = _make_state()
        state["shortcut_registry"] = {
            "1": {"app_id": 100, "name": "Keep"},
            "2": {"name": "Drop"},
            "3": {"app_id": "stringy", "name": "Drop2"},
        }
        service = _make_service(state=state, logger=logger, save_state=save_state)
        service.prune_stale_registry()
        assert "1" in state["shortcut_registry"]
        assert "2" not in state["shortcut_registry"]
        assert "3" not in state["shortcut_registry"]
        save_state.assert_called_once()
