"""Tests for SaveService with FakeSaveApi (no HTTP, no mocking)."""

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fakes.fake_save_api import FakeSaveApi
from fakes.system_time import FakeClock

from adapters.persistence import PersistenceAdapter, SaveSyncStatePersisterAdapter
from adapters.save_file import SaveFileAdapter
from lib.errors import RommApiError
from services.saves import SaveService, SaveServiceConfig


def _make_save_sync_state_persister(tmp_path) -> SaveSyncStatePersisterAdapter:
    """Adapter rooted at tmp_path so disk-touching tests stay end-to-end."""
    return SaveSyncStatePersisterAdapter(
        PersistenceAdapter(
            settings_dir=str(tmp_path),
            runtime_dir=str(tmp_path),
            logger=logging.getLogger("test"),
        )
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _no_retry(fn, *a, **kw):
    return fn(*a, **kw)


def _make_retry():
    retry = MagicMock()
    retry.with_retry.side_effect = _no_retry
    retry.is_retryable.return_value = False
    return retry


_CONFIG_FIELDS = frozenset(
    {
        "runtime_dir",
        "save_sync_state_persister",
        "save_file",
        "loop",
        "logger",
        "clock",
        "get_saves_path",
        "get_roms_path",
        "get_active_core",
        "get_core_name",
        "plugin_version",
        "emit",
        "detect_sort_change",
        "is_retrodeck_migration_pending",
    }
)


def make_service(tmp_path, fake_api=None, *, emit=None, **overrides) -> tuple["SaveService", "FakeSaveApi"]:
    """Create a SaveService with sensible defaults for testing."""
    fake: FakeSaveApi = fake_api or FakeSaveApi()
    config_kwargs: dict[str, Any] = dict(
        runtime_dir=str(tmp_path),
        save_sync_state_persister=_make_save_sync_state_persister(tmp_path),
        save_file=SaveFileAdapter(),
        loop=asyncio.get_event_loop(),
        logger=logging.getLogger("test"),
        clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
        get_saves_path=lambda: str(tmp_path / "saves"),
        get_roms_path=lambda: str(tmp_path / "retrodeck" / "roms"),
        get_active_core=lambda system_name, rom_filename=None: (None, None),
        plugin_version="0.14.0",
        emit=emit,
    )
    ctor_kwargs: dict[str, Any] = dict(
        romm_api=fake,
        retry=_make_retry(),
        settings={"log_level": "debug"},
        state={"shortcut_registry": {}, "installed_roms": {}},
        save_sync_state=SaveService.make_default_state(),
    )
    for key, value in overrides.items():
        if key in _CONFIG_FIELDS:
            config_kwargs[key] = value
        else:
            ctor_kwargs[key] = value
    svc = SaveService(**ctor_kwargs, config=SaveServiceConfig(**config_kwargs))
    svc.init_state()
    return svc, fake


def _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba"):
    """Register a ROM in installed_roms state."""
    svc._state["installed_roms"][str(rom_id)] = {
        "rom_id": rom_id,
        "file_name": file_name,
        "file_path": str(tmp_path / "retrodeck" / "roms" / system / file_name),
        "system": system,
        "platform_slug": system,
        "installed_at": "2026-01-01T00:00:00",
    }


def _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"\x00" * 1024, ext=".srm"):
    """Create a save file on disk and return its path."""
    saves_dir = tmp_path / "saves" / system
    saves_dir.mkdir(parents=True, exist_ok=True)
    save_file = saves_dir / (rom_name + ext)
    save_file.write_bytes(content)
    return save_file


_SERVER_SAVE_SENTINEL = object()


def _server_save(
    save_id=100,
    rom_id=42,
    filename="pokemon.srm",
    updated_at="2026-02-17T06:00:00Z",
    file_size_bytes=1024,
    slot=_SERVER_SAVE_SENTINEL,
    file_name_no_tags=None,
):
    if file_name_no_tags is None:
        # Strip extension to approximate RomM's file_name_no_tags
        file_name_no_tags = filename.rsplit(".", 1)[0] if "." in filename else filename
    result = {
        "id": save_id,
        "rom_id": rom_id,
        "file_name": filename,
        "file_name_no_tags": file_name_no_tags,
        "updated_at": updated_at,
        "file_size_bytes": file_size_bytes,
        "emulator": "retroarch",
        "download_path": f"/saves/{filename}",
    }
    if slot is not _SERVER_SAVE_SENTINEL:
        result["slot"] = slot
    return result


def _file_md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# TestStateManagement
# ---------------------------------------------------------------------------


class TestStateManagement:
    def test_make_default_state(self):
        state = SaveService.make_default_state()
        assert state["device_id"] is None
        assert state["saves"] == {}
        assert state["settings"]["save_sync_enabled"] is False

    def test_init_state_populates_defaults(self, tmp_path):
        svc, _ = make_service(tmp_path, save_sync_state={})
        assert svc._save_sync_state["settings"]["save_sync_enabled"] is False
        assert svc._save_sync_state["saves"] == {}

    def test_init_state_preserves_existing(self, tmp_path):
        state = SaveService.make_default_state()
        state["device_id"] = "existing-id"
        svc, _ = make_service(tmp_path, save_sync_state=state)
        assert svc._save_sync_state["device_id"] == "existing-id"

    def test_load_state_drops_legacy_dismissed_newer_save_id(self, tmp_path):
        """v0.15.0 user state with the obsolete dismissed_newer_save_id field
        gets the field stripped after load_state runs migrations on the
        loaded data. Mirrors the production order init_state → load_state."""
        legacy = {
            "version": 1,
            "device_id": None,
            "saves": {
                "42": {
                    "files": {
                        "game.srm": {
                            "tracked_save_id": 100,
                            "last_sync_hash": "abc",
                            "dismissed_newer_save_id": 200,  # legacy
                        },
                        "game.rtc": {
                            "tracked_save_id": 101,
                            "dismissed_newer_save_id": 201,  # legacy
                        },
                    }
                }
            },
        }
        (tmp_path / "save_sync_state.json").write_text(json.dumps(legacy))

        svc, _ = make_service(tmp_path)  # calls init_state internally
        svc.load_state()

        files = svc._save_sync_state["saves"]["42"]["files"]
        assert "dismissed_newer_save_id" not in files["game.srm"]
        assert "dismissed_newer_save_id" not in files["game.rtc"]
        assert files["game.srm"]["tracked_save_id"] == 100
        assert files["game.srm"]["last_sync_hash"] == "abc"
        assert files["game.rtc"]["tracked_save_id"] == 101

    def test_load_state_drops_legacy_dismissed_newer_save_id_persists_to_disk(self, tmp_path):
        """End-to-end: legacy field on disk → init_state → load_state →
        save_state → reread → field is gone from the file. This is the
        invariant the smoke test (T16) verifies on hardware."""
        legacy = {
            "saves": {
                "42": {
                    "files": {
                        "game.srm": {
                            "tracked_save_id": 100,
                            "dismissed_newer_save_id": 999,
                        }
                    }
                }
            }
        }
        path = tmp_path / "save_sync_state.json"
        path.write_text(json.dumps(legacy))

        svc, _ = make_service(tmp_path)
        svc.load_state()
        svc.save_state()

        on_disk = json.loads(path.read_text())
        assert "dismissed_newer_save_id" not in on_disk["saves"]["42"]["files"]["game.srm"]

    def test_load_state_renames_active_core_to_last_synced_core(self, tmp_path):
        """Legacy ``active_core`` is migrated to ``last_synced_core`` on load."""
        legacy = {
            "saves": {
                "42": {
                    "active_core": "mgba_libretro",
                    "files": {},
                }
            }
        }
        (tmp_path / "save_sync_state.json").write_text(json.dumps(legacy))

        svc, _ = make_service(tmp_path)
        svc.load_state()

        entry = svc._save_sync_state["saves"]["42"]
        assert "active_core" not in entry
        assert entry["last_synced_core"] == "mgba_libretro"

    def test_load_state_skips_migration_for_malformed_entries(self, tmp_path):
        """Migration is defensive: non-dict values don't crash."""
        legacy = {
            "saves": {
                "42": {
                    "files": {
                        "good.srm": {"tracked_save_id": 100, "dismissed_newer_save_id": 5},
                        "weird.srm": "not-a-dict",
                    }
                }
            }
        }
        (tmp_path / "save_sync_state.json").write_text(json.dumps(legacy))

        svc, _ = make_service(tmp_path)
        svc.load_state()  # should not raise

        files = svc._save_sync_state["saves"]["42"]["files"]
        assert "dismissed_newer_save_id" not in files["good.srm"]

    def test_migrate_loaded_state_strips_legacy_settings_keys(self, tmp_path):
        """Legacy ``conflict_mode`` and ``clock_skew_tolerance_sec`` settings
        are dropped on state load. Other settings keys survive."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"] = {
            "conflict_mode": "ask_me",
            "clock_skew_tolerance_sec": 60,
            "save_sync_enabled": True,
        }

        svc._migrate_loaded_state()

        settings = svc._save_sync_state["settings"]
        assert "conflict_mode" not in settings
        assert "clock_skew_tolerance_sec" not in settings
        assert settings["save_sync_enabled"] is True

    def test_migrate_loaded_state_strip_legacy_settings_idempotent(self, tmp_path):
        """Stripping legacy settings is a no-op when they aren't present."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"] = {"save_sync_enabled": True}

        svc._migrate_loaded_state()  # should not raise

        assert svc._save_sync_state["settings"] == {"save_sync_enabled": True}

    def test_migrate_loaded_state_handles_missing_settings(self, tmp_path):
        """Migration is defensive: missing ``settings`` key doesn't crash."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.pop("settings", None)

        svc._migrate_loaded_state()  # should not raise

        assert "settings" not in svc._save_sync_state

    def test_migrate_loaded_state_handles_non_dict_settings(self, tmp_path):
        """Migration is defensive: non-dict ``settings`` is left untouched."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"] = "broken"

        svc._migrate_loaded_state()  # should not raise

        assert svc._save_sync_state["settings"] == "broken"

    def test_save_and_load_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["device_id"] = "test-device"
        svc._save_sync_state["saves"]["42"] = {"files": {}}
        svc.save_state()

        # Load into a fresh service
        svc2, _ = make_service(tmp_path)
        svc2.load_state()
        assert svc2._save_sync_state["device_id"] == "test-device"
        assert "42" in svc2._save_sync_state["saves"]

    def test_load_state_missing_file(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc.load_state()  # should not raise
        assert svc._save_sync_state["device_id"] is None

    def test_prune_orphaned_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["saves"]["99"] = {"files": {}}
        svc._save_sync_state["playtime"]["99"] = {"total_seconds": 100}
        svc._state["shortcut_registry"]["42"] = {}

        svc.prune_orphaned_state()
        assert "99" not in svc._save_sync_state["saves"]
        assert "99" not in svc._save_sync_state["playtime"]

    def test_prune_keeps_registered(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["saves"]["42"] = {"files": {}}
        svc._state["shortcut_registry"]["42"] = {}

        svc.prune_orphaned_state()
        assert "42" in svc._save_sync_state["saves"]


# ---------------------------------------------------------------------------
# TestDeviceRegistration
# ---------------------------------------------------------------------------


class TestDeviceRegistration:
    @pytest.mark.asyncio
    async def test_registers_new_device(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        result = await svc.ensure_device_registered()
        assert result["success"] is True
        assert result["device_id"]
        assert result["device_name"]
        # Persisted
        assert svc._save_sync_state["device_id"] == result["device_id"]

    @pytest.mark.asyncio
    async def test_returns_existing_device(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "existing"
        svc._save_sync_state["device_name"] = "deck"
        svc._save_sync_state["server_device_id"] = "server-existing"

        result = await svc.ensure_device_registered()
        assert result["device_id"] == "existing"
        assert result["device_name"] == "deck"
        assert result["server_device_id"] == "server-existing"

    @pytest.mark.asyncio
    async def test_disabled_returns_failure(self, tmp_path):
        svc, _ = make_service(tmp_path)
        # save_sync_enabled defaults to False
        result = await svc.ensure_device_registered()
        assert result["success"] is False
        assert result.get("disabled") is True


# ---------------------------------------------------------------------------
# TestDeviceRegistrationV47
# ---------------------------------------------------------------------------


class TestDeviceRegistrationServer:
    @pytest.mark.asyncio
    async def test_registers_with_server(self, tmp_path):
        """Calls register_device and stores server_device_id."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        result = await svc.ensure_device_registered()
        assert result["success"] is True
        assert result.get("server_device_id") is not None
        assert svc._save_sync_state["server_device_id"] == result["server_device_id"]
        # Verify register_device was called
        reg_calls = [c for c in fake.call_log if c[0] == "register_device"]
        assert len(reg_calls) == 1
        assert reg_calls[0][1][0]  # name (hostname)
        assert reg_calls[0][1][1] == "linux"  # platform
        assert reg_calls[0][1][2] == "decky-romm-sync"  # client

    @pytest.mark.asyncio
    async def test_returns_failure_on_server_error(self, tmp_path):
        """If register_device fails, returns failure."""
        fake = FakeSaveApi()
        fake.fail_on_next(Exception("server error"))
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        result = await svc.ensure_device_registered()
        assert result["success"] is False
        assert result.get("error") == "registration_failed"
        assert svc._save_sync_state.get("server_device_id") is None

    @pytest.mark.asyncio
    async def test_returns_existing_with_server_device_id(self, tmp_path):
        """If already registered, returns existing IDs including server_device_id."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "existing-id"
        svc._save_sync_state["device_name"] = "deck"
        svc._save_sync_state["server_device_id"] = "server-id-123"

        result = await svc.ensure_device_registered()
        assert result["device_id"] == "existing-id"
        assert result.get("server_device_id") == "server-id-123"

    @pytest.mark.asyncio
    async def test_upgrades_local_uuid_to_server(self, tmp_path):
        """Local-only UUID gets upgraded to server registration."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        # Simulate existing local-only UUID (from failed registration)
        svc._save_sync_state["device_id"] = "local-only-uuid"
        svc._save_sync_state["device_name"] = "deck"
        svc._save_sync_state["server_device_id"] = None

        result = await svc.ensure_device_registered()
        assert result["success"] is True
        assert result.get("server_device_id") is not None
        assert svc._save_sync_state["server_device_id"] is not None
        # register_device was called
        reg_calls = [c for c in fake.call_log if c[0] == "register_device"]
        assert len(reg_calls) == 1

    @pytest.mark.asyncio
    async def test_ensure_device_registered_reconciles_client_version(self, tmp_path):
        """Already-registered path calls update_device with current plugin_version."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "existing-id"
        svc._save_sync_state["device_name"] = "deck"
        svc._save_sync_state["server_device_id"] = "server-abc"

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        update_calls = [c for c in fake.call_log if c[0] == "update_device"]
        assert len(update_calls) == 1
        assert update_calls[0][1][0] == "server-abc"
        assert update_calls[0][2].get("client_version") == "0.14.0"

    @pytest.mark.asyncio
    async def test_ensure_device_registered_reconcile_non_fatal(self, tmp_path):
        """PUT raises, ensure_device_registered still returns success."""
        fake = FakeSaveApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "existing-id"
        svc._save_sync_state["device_name"] = "deck"
        svc._save_sync_state["server_device_id"] = "server-abc"

        # Make update_device fail silently
        fake.fail_on_next(Exception("network error"))
        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "existing-id"

    @pytest.mark.asyncio
    async def test_probes_version_when_unset(self, tmp_path):
        """ensure_device_registered probes the version when adapter has none."""

        class VersionedFakeApi(FakeSaveApi):
            def __init__(self) -> None:
                super().__init__()
                self.heartbeat_calls = 0

            def heartbeat(self) -> dict:
                self.heartbeat_calls += 1
                return {"SYSTEM": {"VERSION": "4.8.5"}}

        fake = VersionedFakeApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        await svc.ensure_device_registered()

        assert fake.heartbeat_calls == 1
        assert fake.get_version() == "4.8.5"

    @pytest.mark.asyncio
    async def test_skips_probe_when_version_already_set(self, tmp_path):
        """No probe when adapter already has a version."""

        class VersionedFakeApi(FakeSaveApi):
            def __init__(self) -> None:
                super().__init__()
                self.heartbeat_calls = 0

            def heartbeat(self) -> dict:
                self.heartbeat_calls += 1
                return {"SYSTEM": {"VERSION": "4.8.5"}}

        fake = VersionedFakeApi()
        fake.set_version("4.8.1")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        await svc.ensure_device_registered()

        assert fake.heartbeat_calls == 0
        assert fake.get_version() == "4.8.1"

    @pytest.mark.asyncio
    async def test_probe_failure_is_non_fatal(self, tmp_path):
        """Heartbeat failure during version probe does not prevent registration."""
        fake = FakeSaveApi()
        fake.heartbeat_raises = ConnectionError("offline")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        result = await svc.ensure_device_registered()

        assert result["success"] is True
        assert fake.get_version() is None

    @pytest.mark.asyncio
    async def test_probe_skipped_when_disabled(self, tmp_path):
        """Disabled save sync short-circuits before any probe."""

        class VersionedFakeApi(FakeSaveApi):
            def __init__(self) -> None:
                super().__init__()
                self.heartbeat_calls = 0

            def heartbeat(self) -> dict:
                self.heartbeat_calls += 1
                return {"SYSTEM": {"VERSION": "4.8.5"}}

        fake = VersionedFakeApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        # save_sync_enabled defaults to False
        result = await svc.ensure_device_registered()

        assert result["success"] is False
        assert result.get("disabled") is True
        assert fake.heartbeat_calls == 0


# ---------------------------------------------------------------------------
# TestListDevices
# ---------------------------------------------------------------------------


class TestListDevices:
    @pytest.mark.asyncio
    async def test_list_devices_marks_own_device(self, tmp_path):
        """own device_id present in state — is_current_device is True on matching entry."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["server_device_id"] = "device-1"

        # Register two devices in fake
        fake._registered_devices = [
            {"id": "device-1", "name": "steamdeck"},
            {"id": "device-2", "name": "laptop"},
        ]

        result = await svc.list_devices()

        assert result["success"] is True
        assert len(result["devices"]) == 2
        own = next(d for d in result["devices"] if d["id"] == "device-1")
        other = next(d for d in result["devices"] if d["id"] == "device-2")
        assert own["is_current_device"] is True
        assert other["is_current_device"] is False

    @pytest.mark.asyncio
    async def test_list_devices_save_sync_disabled(self, tmp_path):
        """Returns disabled=True when save sync is off."""
        svc, _ = make_service(tmp_path)
        # save_sync_enabled defaults to False

        result = await svc.list_devices()

        assert result == {"success": False, "devices": [], "disabled": True}

    @pytest.mark.asyncio
    async def test_list_devices_adapter_error(self, tmp_path):
        """Adapter raises — returns error response."""
        fake = FakeSaveApi()
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        fake.fail_on_next(Exception("server unavailable"))
        result = await svc.list_devices()

        assert result == {"success": False, "devices": [], "error": "list_failed"}

    @pytest.mark.asyncio
    async def test_list_devices_no_own_id_all_false(self, tmp_path):
        """No server_device_id in state — all is_current_device are False."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["server_device_id"] = None

        fake._registered_devices = [{"id": "device-1", "name": "steamdeck"}]
        result = await svc.list_devices()

        assert result["success"] is True
        assert result["devices"][0]["is_current_device"] is False

    @pytest.mark.asyncio
    async def test_list_devices_handles_null_id(self, tmp_path):
        """Device with id=None must not match own_id=None (avoid 'None'=='None' trap)."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["server_device_id"] = None

        fake._registered_devices = [{"id": None, "name": "unknown"}]
        result = await svc.list_devices()

        assert result["success"] is True
        # id=None and own_id=None must both resolve to "" — empty string never
        # compares truthy, so is_current_device must be False
        assert result["devices"][0]["is_current_device"] is False


# ---------------------------------------------------------------------------
# TestSyncRomSaves
# ---------------------------------------------------------------------------


class TestSyncRomSaves:
    def test_local_only_uploads(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"save data")

        synced, errors, conflicts = svc._sync_rom_saves(42)
        assert synced == 1
        assert errors == []
        assert conflicts == []
        assert any(c[0] == "upload_save" for c in fake.call_log)

    def test_server_only_downloads(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        # Add server save but no local file
        ss = _server_save()
        fake.saves[100] = ss

        synced, errors, _ = svc._sync_rom_saves(42)
        assert synced == 1
        assert errors == []
        # Verify the file was downloaded
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()

    def test_rom_not_installed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        synced, errors, _ = svc._sync_rom_saves(999)
        assert synced == 0
        assert errors == []

    def test_api_error_on_list_saves(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        fake.fail_on_next(RommApiError("Server error"))

        synced, errors, _ = svc._sync_rom_saves(42)
        assert synced == 0
        assert len(errors) == 1
        assert "Failed to fetch saves" in errors[0]

    # ------------------------------------------------------------------
    # Regression tests for issue #238 — pending-migration handling.
    # Rule 2: skip server_only downloads while a save-sort migration is
    # pending so the mtime-naive resolver cannot prefer freshly-downloaded
    # stale server content over real user progress at the other layout.
    # ------------------------------------------------------------------

    def test_sync_rom_saves_skips_server_only_downloads_during_pending_migration(self, tmp_path):
        """server_only matches must be skipped while migration is pending (#238)."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        # Mark migration pending — detect has fired, user hasn't resolved yet.
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": False}
        svc._state["save_sort_settings_previous"] = {"sort_by_content": True, "sort_by_core": False}
        # Server has a save, no local file anywhere.
        ss = _server_save()
        fake.saves[100] = ss

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 0
        assert errors == []
        assert conflicts == []
        # No download was initiated.
        assert fake.downloaded_files == {}
        # No file landed on disk under either layout.
        saves_dir = tmp_path / "saves" / "gba"
        assert not (saves_dir / "pokemon.srm").exists()

    def test_sync_rom_saves_uploads_local_only_during_pending_migration(self, tmp_path):
        """local_only matches must still upload during pending migration (#238)."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": False}
        svc._state["save_sort_settings_previous"] = {"sort_by_content": True, "sort_by_core": False}
        # Local save at the (previous == current, same layout) location.
        _create_save(tmp_path, content=b"user progress")

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        # Upload went through.
        assert any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_rom_saves_invokes_detect_sort_change_before_sync(self, tmp_path):
        """Manual sync_rom_saves must also refresh save-sort state first (#238).

        Without the detect-first call, a user editing retroarch.cfg outside
        of a session and then triggering manual sync would race the same
        way that direct-Steam-launch does — sync would compute saves_dir
        from stale state and risk landing stale server content at the
        wrong layout.
        """
        call_order: list[str] = []

        def fake_detect() -> None:
            call_order.append("detect")

        svc, _ = make_service(tmp_path, detect_sort_change=fake_detect)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        orig_sync = svc._sync_rom_saves

        def wrapped_sync(rom_id):
            call_order.append("sync")
            return orig_sync(rom_id)

        svc._sync_rom_saves = wrapped_sync  # type: ignore[method-assign]

        result = await svc.sync_rom_saves(42)

        assert result["success"] is True
        # detect fired exactly once, before sync ran.
        assert call_order.count("detect") == 1
        assert call_order.index("detect") < call_order.index("sync")

    @pytest.mark.asyncio
    async def test_sync_rom_saves_message_includes_conflict_count(self, tmp_path):
        """Public sync_rom_saves must surface conflict count in its message.

        Previously reported "Synced 0 save(s)" even with conflicts present,
        which reads as success — user had no signal that manual intervention
        was needed.
        """
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)

        # Stub _sync_rom_saves to return 1 conflict, 0 synced, 0 errors
        def stub_sync(rom_id):
            return (0, [], [{"type": "newer_in_slot", "rom_id": rom_id}])

        svc._sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.sync_rom_saves(42)

        # success is still True — conflicts are legitimate state, not technical failure
        assert result["success"] is True
        assert "1 conflict(s)" in result["message"]
        assert result["synced"] == 0


# ---------------------------------------------------------------------------
# TestSyncAllSaves
# ---------------------------------------------------------------------------


class TestSyncAllSaves:
    @pytest.mark.asyncio
    async def test_syncs_multiple_roms(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"

        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"save2")

        result = await svc.sync_all_saves()
        assert result["success"] is True
        assert result["synced"] == 2
        assert result["roms_checked"] == 2

    @pytest.mark.asyncio
    async def test_disabled_returns_early(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.sync_all_saves()
        assert result["success"] is False
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_partial_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"

        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"save2")

        # Make the second ROM's list_saves fail
        original_list = fake.list_saves

        call_count = 0

        def flaky_list(rom_id, *, device_id=None, slot=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RommApiError("Server error")
            return original_list(rom_id, device_id=device_id, slot=slot)

        fake.list_saves = flaky_list

        result = await svc.sync_all_saves()
        assert result["synced"] >= 1
        assert len(result["errors"]) >= 1

    @pytest.mark.asyncio
    async def test_sync_all_saves_invokes_detect_sort_change_before_sync(self, tmp_path):
        """Manual sync_all_saves must also refresh save-sort state first (#238).

        Same race as sync_rom_saves but for the bulk path: detect must
        fire once at the top of the method, before any per-ROM sync runs.
        """
        call_order: list[str] = []

        def fake_detect() -> None:
            call_order.append("detect")

        svc, _ = make_service(tmp_path, detect_sort_change=fake_detect)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")

        orig_sync = svc._sync_rom_saves

        def wrapped_sync(rom_id):
            call_order.append("sync")
            return orig_sync(rom_id)

        svc._sync_rom_saves = wrapped_sync  # type: ignore[method-assign]

        result = await svc.sync_all_saves()

        assert result["success"] is True
        # detect fired exactly once, before any per-ROM sync ran.
        assert call_order.count("detect") == 1
        assert call_order.index("detect") < call_order.index("sync")

    @pytest.mark.asyncio
    async def test_sync_all_saves_success_stays_true_with_only_conflicts(self, tmp_path):
        """Regression guard: success flag reflects errors only, not conflicts.

        Conflicts are a legitimate state requiring user resolution — not a
        technical failure. Frontend distinguishes via conflicts count; success
        flag must stay reserved for actual errors.
        """
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")

        # Stub internal sync to produce conflicts but no errors
        def stub_sync(rom_id):
            return (0, [], [{"type": "newer_in_slot", "rom_id": rom_id}])

        svc._sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.sync_all_saves()

        assert result["success"] is True
        assert result["conflicts"] >= 1
        assert "conflict(s)" in result["message"]


# ---------------------------------------------------------------------------
# TestPreLaunchSync
# ---------------------------------------------------------------------------


class TestPreLaunchSync:
    @pytest.mark.asyncio
    async def test_downloads_server_saves(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)

        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.pre_launch_sync(42)
        assert result["success"] is True
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_disabled_skips(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.pre_launch_sync(42)
        assert result["success"] is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_pre_launch_disabled_in_settings(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["settings"]["sync_before_launch"] = False
        svc._save_sync_state["device_id"] = "test-device"

        result = await svc.pre_launch_sync(42)
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_pre_launch_sync_invokes_detect_sort_change_before_migration_gate(self, tmp_path):
        """detect_sort_change is called before the _is_save_sort_changed gate (#238)."""
        order: list[str] = []

        def fake_detect() -> None:
            # Simulate detect discovering a pending migration.
            order.append("detect")

        svc, _ = make_service(tmp_path, detect_sort_change=fake_detect)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"

        # Track when _is_save_sort_changed is consulted.
        orig_gate = svc._is_save_sort_changed

        def wrapped_gate():
            order.append("gate")
            return orig_gate()

        svc._is_save_sort_changed = wrapped_gate  # type: ignore[method-assign]

        await svc.pre_launch_sync(42)

        assert "detect" in order
        assert "gate" in order
        assert order.index("detect") < order.index("gate")


class TestRetroDeckMigrationBlocksSaveSync:
    @pytest.mark.asyncio
    async def test_pre_launch_sync_skips_when_retrodeck_migration_pending(self, tmp_path):
        svc, _ = make_service(tmp_path, is_retrodeck_migration_pending=lambda: True)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert result["blocked_by_migration"] is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_post_exit_sync_skips_when_retrodeck_migration_pending(self, tmp_path):
        svc, _ = make_service(tmp_path, is_retrodeck_migration_pending=lambda: True)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result["blocked_by_migration"] is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_sync_all_saves_respects_migration_block_via_decorator_chain(self, tmp_path):
        """End-to-end chain check: Plugin.sync_all_saves must be blocked by the
        @migration_blocked decorator before SaveService.sync_all_saves runs, so
        the internal _sync_rom_saves call path is never reached when migration
        is pending. Protects against accidental decorator removal at the public
        callable layer."""
        from main import Plugin

        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)

        plugin = Plugin()
        plugin._save_sync_service = svc
        plugin._migration_service = MagicMock()
        plugin._migration_service.is_retrodeck_migration_pending.return_value = True

        spy = MagicMock(name="_sync_rom_saves_spy")
        svc._sync_rom_saves = spy  # type: ignore[method-assign]

        result = await plugin.sync_all_saves()

        assert result["blocked_by_migration"] is True
        assert result["success"] is False
        spy.assert_not_called()


# ---------------------------------------------------------------------------
# TestPostExitSync
# ---------------------------------------------------------------------------


class TestPostExitSync:
    @pytest.mark.asyncio
    async def test_uploads_changed_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"new save data")

        result = await svc.post_exit_sync(42)
        assert result["success"] is True
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_disabled_skips(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.post_exit_sync(42)
        assert result["success"] is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_post_exit_disabled_in_settings(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["settings"]["sync_after_exit"] = False
        svc._save_sync_state["device_id"] = "test-device"

        result = await svc.post_exit_sync(42)
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_auto_registers_device(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        # No device_id set
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")

        result = await svc.post_exit_sync(42)
        assert result["success"] is True
        assert svc._save_sync_state["device_id"] is not None

    # ------------------------------------------------------------------
    # Regression tests for issue #238 — detect-first invariant.
    #
    # Save-sync must refresh save-sort state via detect_sort_change
    # before computing saves_dir, so that Rule 1 / Rule 2 engage even
    # when a direct-Steam-launch race delivers post_exit_sync before
    # refreshMigrationState. See #238.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_post_exit_sync_invokes_detect_sort_change_before_sync(self, tmp_path):
        """detect_sort_change is called exactly once before the sync path runs (#238)."""
        call_order: list[str] = []

        def fake_detect() -> None:
            call_order.append("detect")

        svc, _ = make_service(tmp_path, detect_sort_change=fake_detect)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        # Patch _sync_rom_saves to record call ordering.
        orig_sync = svc._sync_rom_saves

        def wrapped_sync(rom_id):
            call_order.append("sync")
            return orig_sync(rom_id)

        svc._sync_rom_saves = wrapped_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is True
        # detect fired exactly once, before sync ran.
        assert call_order.count("detect") == 1
        assert call_order.index("detect") < call_order.index("sync")

    @pytest.mark.asyncio
    async def test_post_exit_sync_continues_when_detect_sort_change_raises(self, tmp_path, caplog):
        """If detect_sort_change raises, save-sync logs a warning and proceeds (#238)."""

        def boom() -> None:
            raise RuntimeError("cfg file unreadable")

        svc, _ = make_service(tmp_path, detect_sort_change=boom)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        with caplog.at_level(logging.WARNING, logger="test"):
            result = await svc.post_exit_sync(42)

        assert result["success"] is True
        # Sync still ran despite detect failure.
        assert result["synced"] == 1
        # Warning was logged.
        assert any("detect_sort_change failed" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_post_exit_sync_works_when_detect_sort_change_is_none(self, tmp_path):
        """Default detect_sort_change=None: post_exit_sync still runs without error (#238)."""
        svc, _ = make_service(tmp_path)  # detect_sort_change not passed → None
        assert svc._detect_sort_change is None
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        result = await svc.post_exit_sync(42)

        assert result["success"] is True
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_post_exit_sync_message_includes_conflict_count(self, tmp_path):
        """post_exit_sync must surface conflict count in its message.

        Previously "Uploaded 0 save(s)" even with conflicts — user has no
        signal that sync is blocked on manual resolution.
        """
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id):
            return (0, [], [{"type": "newer_in_slot", "rom_id": rom_id}])

        svc._sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is True
        assert "1 conflict(s)" in result["message"]
        assert result["synced"] == 0


# ---------------------------------------------------------------------------
# TestPostExitSyncConnectivity
# ---------------------------------------------------------------------------


class TestPostExitSyncConnectivity:
    @pytest.mark.asyncio
    async def test_returns_offline_when_heartbeat_fails(self, tmp_path):
        """post_exit_sync returns offline=True when server is unreachable."""
        fake = FakeSaveApi()
        fake.heartbeat_raises = ConnectionError("unreachable")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result.get("offline") is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_proceeds_when_heartbeat_succeeds(self, tmp_path):
        """post_exit_sync proceeds normally when server is reachable."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"save data")

        result = await svc.post_exit_sync(42)

        assert result.get("offline") is not True
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_offline_skips_before_device_registration(self, tmp_path):
        """post_exit_sync returns offline without attempting device registration."""
        fake = FakeSaveApi()
        fake.heartbeat_raises = OSError("connection refused")
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        # No device_id — would trigger registration if heartbeat passed

        result = await svc.post_exit_sync(42)

        assert result.get("offline") is True
        # Device should not have been registered
        assert not svc._save_sync_state.get("device_id")


# ---------------------------------------------------------------------------
# TestSaveStatus
# ---------------------------------------------------------------------------


class TestSaveStatus:
    @pytest.mark.asyncio
    async def test_get_save_status(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.get_save_status(42)
        assert result["rom_id"] == 42
        assert len(result["files"]) >= 1

    @pytest.mark.asyncio
    async def test_get_save_status_no_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = await svc.get_save_status(42)
        assert result["rom_id"] == 42
        assert result["files"] == []

    @pytest.mark.asyncio
    async def test_get_save_status_includes_empty_conflicts_when_no_conflict(self, tmp_path):
        """get_save_status response includes conflicts key (empty when no conflicts)."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = await svc.get_save_status(42)
        assert "conflicts" in result
        assert result["conflicts"] == []

    @pytest.mark.asyncio
    async def test_get_save_status_includes_device_syncs(self, tmp_path):
        """get_save_status includes device_syncs and is_current per file."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        svc._save_sync_state["server_device_id"] = "server-dev-1"
        svc._save_sync_state["device_id"] = "server-dev-1"

        ss = _server_save()
        ss["device_syncs"] = [
            {
                "device_id": "server-dev-1",
                "device_name": "my-deck",
                "is_current": True,
                "last_synced_at": "2026-03-24T10:00:00",
            },
            {
                "device_id": "server-dev-2",
                "device_name": "desktop",
                "is_current": False,
                "last_synced_at": "2026-03-24T08:00:00",
            },
        ]
        fake.saves[100] = ss

        result = await svc.get_save_status(42)
        file_status = result["files"][0]
        assert "device_syncs" in file_status
        assert len(file_status["device_syncs"]) == 2
        assert file_status["device_syncs"][0]["device_name"] == "my-deck"
        assert file_status["is_current"] is True

    @pytest.mark.asyncio
    async def test_save_status_filters_by_active_slot(self, tmp_path):
        """Saves from a different slot should not appear in status."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        # Server save in slot "default", but active_slot is "other"
        ss = _server_save(slot="default")
        fake.saves[100] = ss
        svc._save_sync_state["saves"]["42"] = {"active_slot": "other", "files": {}}

        result = await svc.get_save_status(42)
        # Local file exists → should show as upload (local-only), not synced against wrong slot
        assert len(result["files"]) == 1
        assert result["files"][0]["status"] == "upload"
        assert result["files"][0]["server_save_id"] is None


# ---------------------------------------------------------------------------
# TestCheckSaveStatusBackground
# ---------------------------------------------------------------------------


class TestCheckSaveStatusBackground:
    """Tests for the background save status check with event emit."""

    @pytest.mark.asyncio
    async def test_emits_save_status_updated(self, tmp_path):
        """Background check runs full status and emits result."""
        emitted = []

        async def fake_emit(event, *args):
            emitted.append((event, args))

        svc, _fake = make_service(tmp_path, emit=fake_emit)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        await svc.check_save_status_background(42)

        assert len(emitted) == 1
        assert emitted[0][0] == "save_status_updated"
        result = emitted[0][1][0]
        assert result["rom_id"] == 42
        assert len(result["files"]) >= 1

    @pytest.mark.asyncio
    async def test_no_emit_when_emit_is_none(self, tmp_path):
        """Background check works without emit (no crash)."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        # Should not raise even without emit
        await svc.check_save_status_background(42)

    @pytest.mark.asyncio
    async def test_swallows_errors(self, tmp_path):
        """Background check logs but does not raise on errors."""
        svc, fake = make_service(tmp_path)
        fake.fail_on_next(Exception("Server down"))

        # Should not raise
        await svc.check_save_status_background(42)


# ---------------------------------------------------------------------------
# TestSettings
# ---------------------------------------------------------------------------


class TestSettings:
    @pytest.mark.asyncio
    async def test_get_defaults(self, tmp_path):
        svc, _ = make_service(tmp_path)
        settings = svc.get_save_sync_settings()
        assert settings["save_sync_enabled"] is False
        assert settings["sync_before_launch"] is True
        assert settings["sync_after_exit"] is True

    @pytest.mark.asyncio
    async def test_update_settings(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.update_save_sync_settings(
            {
                "save_sync_enabled": True,
                "sync_before_launch": False,
            }
        )
        assert result["success"] is True
        assert result["settings"]["save_sync_enabled"] is True
        assert result["settings"]["sync_before_launch"] is False

    @pytest.mark.asyncio
    async def test_unknown_key_ignored(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.update_save_sync_settings({"unknown_key": "value"})
        assert result["success"] is True
        assert "unknown_key" not in result["settings"]


# ---------------------------------------------------------------------------
# TestDeleteSaves
# ---------------------------------------------------------------------------


class TestDeleteSaves:
    @pytest.mark.asyncio
    async def test_delete_local_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        assert save_path.exists()

        svc._save_sync_state["saves"]["42"] = {"files": {"pokemon.srm": {}}}

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not save_path.exists()
        # Entry survives — only files are cleared.
        assert "42" in svc._save_sync_state["saves"]
        assert svc._save_sync_state["saves"]["42"]["files"] == {}

    @pytest.mark.asyncio
    async def test_delete_local_saves_preserves_slot_config(self, tmp_path):
        """Slot config and attribution metadata survive a delete (#279)."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        assert save_path.exists()

        svc._save_sync_state["saves"]["42"] = {
            "files": {"pokemon.srm": {"last_sync_hash": "abc"}},
            "active_slot": "desktop",
            "slot_confirmed": True,
            "emulator": "retroarch-mgba",
            "last_synced_core": "mgba_libretro",
            "own_upload_ids": ["save-1", "save-2"],
            "slots": {"default": {}, "desktop": {}},
            "system": "gba",
        }

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not save_path.exists()

        entry = svc._save_sync_state["saves"]["42"]
        assert entry["files"] == {}
        assert entry["active_slot"] == "desktop"
        assert entry["slot_confirmed"] is True
        assert entry["emulator"] == "retroarch-mgba"
        assert entry["last_synced_core"] == "mgba_libretro"
        assert entry["own_upload_ids"] == ["save-1", "save-2"]
        assert entry["slots"] == {"default": {}, "desktop": {}}
        assert entry["system"] == "gba"

    @pytest.mark.asyncio
    async def test_delete_local_saves_no_prior_state_entry(self, tmp_path):
        """Delete on a ROM with no prior saves entry creates a stable empty entry."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # No svc._save_sync_state["saves"]["42"] set up.
        assert "42" not in svc._save_sync_state["saves"]

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 1
        assert not save_path.exists()
        # clear_files_state creates an empty entry with files={}.
        assert svc._save_sync_state["saves"]["42"] == {"files": {}}

    @pytest.mark.asyncio
    async def test_delete_no_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = svc.delete_local_saves(42)
        assert result["success"] is True
        assert result["deleted_count"] == 0


# ---------------------------------------------------------------------------
# TestEmulatorTag
# ---------------------------------------------------------------------------


class TestEmulatorTag:
    def test_upload_uses_emulator_tag_from_core(self, tmp_path):
        """When core resolver returns a core, upload uses retroarch-{core} tag."""
        svc, fake = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
        )
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        svc._do_upload_save(42, str(tmp_path / "saves" / "gba" / "pokemon.srm"), "pokemon.srm", "42", "gba")

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        _name, args, _kwargs = upload_calls[0]
        assert args[2] == "retroarch-mgba"  # emulator argument

    def test_upload_uses_fallback_when_no_core(self, tmp_path):
        """When core resolver returns None, upload falls back to 'retroarch'."""
        svc, fake = make_service(tmp_path)  # default: get_active_core returns (None, None)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        svc._do_upload_save(42, str(tmp_path / "saves" / "gba" / "pokemon.srm"), "pokemon.srm", "42", "gba")

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        _name, args, _kwargs = upload_calls[0]
        assert args[2] == "retroarch"

    @pytest.mark.asyncio
    async def test_delete_platform_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="gba", file_name="game2.gba")
        _create_save(tmp_path, system="gba", rom_name="game1")
        _create_save(tmp_path, system="gba", rom_name="game2")

        result = svc.delete_platform_saves("gba")
        assert result["success"] is True
        assert result["deleted_count"] == 2

    @pytest.mark.asyncio
    async def test_delete_platform_saves_preserves_slot_config(self, tmp_path):
        """Per-platform delete preserves slot config for every affected ROM (#279)."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="gba", file_name="game2.gba")
        _create_save(tmp_path, system="gba", rom_name="game1")
        _create_save(tmp_path, system="gba", rom_name="game2")

        svc._save_sync_state["saves"]["1"] = {
            "files": {"game1.srm": {}},
            "active_slot": "desktop",
            "slot_confirmed": True,
            "emulator": "retroarch-mgba",
            "system": "gba",
        }
        svc._save_sync_state["saves"]["2"] = {
            "files": {"game2.srm": {}},
            "active_slot": "default",
            "slot_confirmed": True,
            "own_upload_ids": ["save-x"],
            "system": "gba",
        }

        result = svc.delete_platform_saves("gba")
        assert result["success"] is True
        assert result["deleted_count"] == 2

        entry1 = svc._save_sync_state["saves"]["1"]
        assert entry1["files"] == {}
        assert entry1["active_slot"] == "desktop"
        assert entry1["slot_confirmed"] is True
        assert entry1["emulator"] == "retroarch-mgba"

        entry2 = svc._save_sync_state["saves"]["2"]
        assert entry2["files"] == {}
        assert entry2["active_slot"] == "default"
        assert entry2["slot_confirmed"] is True
        assert entry2["own_upload_ids"] == ["save-x"]

    @pytest.mark.asyncio
    async def test_delete_platform_saves_other_platform_untouched(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1")
        snes_save = _create_save(tmp_path, system="snes", rom_name="game2")

        svc._save_sync_state["saves"]["2"] = {
            "files": {"game2.srm": {}},
            "active_slot": "default",
            "slot_confirmed": True,
            "system": "snes",
        }

        svc.delete_platform_saves("gba")
        assert snes_save.exists()
        # Other-platform entry must be entirely untouched.
        snes_entry = svc._save_sync_state["saves"]["2"]
        assert snes_entry["files"] == {"game2.srm": {}}
        assert snes_entry["active_slot"] == "default"
        assert snes_entry["slot_confirmed"] is True


# ---------------------------------------------------------------------------
# TestFindSaveFiles
# ---------------------------------------------------------------------------


class TestFindSaveFiles:
    """Tests for _find_save_files."""

    def test_finds_srm(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, system="gba", rom_name="pokemon")

        result = svc._find_save_files(42)

        assert len(result) == 1
        assert result[0]["filename"] == "pokemon.srm"
        assert result[0]["path"].endswith("pokemon.srm")

    def test_finds_rtc_companion(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, file_name="emerald.gba")
        _create_save(tmp_path, system="gba", rom_name="emerald", ext=".srm")
        _create_save(tmp_path, system="gba", rom_name="emerald", ext=".rtc", content=b"\x02" * 16)

        result = svc._find_save_files(42)

        filenames = sorted(f["filename"] for f in result)
        assert filenames == ["emerald.rtc", "emerald.srm"]

    def test_multi_disc_uses_m3u_name(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._state["installed_roms"]["55"] = {
            "rom_id": 55,
            "file_name": "FF7.zip",
            "file_path": str(tmp_path / "retrodeck" / "roms" / "psx" / "FF7" / "Final Fantasy VII.m3u"),
            "system": "psx",
            "platform_slug": "psx",
            "rom_dir": str(tmp_path / "retrodeck" / "roms" / "psx" / "FF7"),
            "installed_at": "2026-01-01T00:00:00",
        }
        # With sort_by_content=True, saves land in saves_base/{content_dir} where
        # content_dir = last folder component of the ROM's directory = "FF7"
        saves_dir = tmp_path / "saves" / "FF7"
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / "Final Fantasy VII.srm").write_bytes(b"\x00" * 1024)

        result = svc._find_save_files(55)

        assert any(f["filename"] == "Final Fantasy VII.srm" for f in result)

    def test_no_save_file_returns_empty(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=10, system="n64", file_name="zelda.z64")
        (tmp_path / "saves" / "n64").mkdir(parents=True, exist_ok=True)

        result = svc._find_save_files(10)

        assert result == []

    def test_saves_dir_not_exists_returns_empty(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = svc._find_save_files(42)

        assert result == []

    def test_rom_not_installed_returns_empty(self, tmp_path):
        svc, _ = make_service(tmp_path)

        result = svc._find_save_files(999)

        assert result == []


# ---------------------------------------------------------------------------
# TestFileMd5
# ---------------------------------------------------------------------------


class TestFileMd5:
    """Tests for _file_md5."""

    def test_known_content(self, tmp_path):
        svc, _ = make_service(tmp_path)
        f = tmp_path / "test.srm"
        content = b"Hello, save file!"
        f.write_bytes(content)

        assert svc._file_md5(str(f)) == hashlib.md5(content).hexdigest()

    def test_empty_file(self, tmp_path):
        svc, _ = make_service(tmp_path)
        f = tmp_path / "empty.srm"
        f.write_bytes(b"")

        assert svc._file_md5(str(f)) == hashlib.md5(b"").hexdigest()

    def test_large_file_chunked(self, tmp_path):
        svc, _ = make_service(tmp_path)
        f = tmp_path / "large.srm"
        content = os.urandom(2 * 1024 * 1024)
        f.write_bytes(content)

        assert svc._file_md5(str(f)) == hashlib.md5(content).hexdigest()

    def test_permission_error(self, tmp_path):
        svc, _ = make_service(tmp_path)
        f = tmp_path / "locked.srm"
        f.write_bytes(b"data")
        f.chmod(0o000)

        try:
            with pytest.raises(PermissionError):
                svc._file_md5(str(f))
        finally:
            f.chmod(0o644)


# ---------------------------------------------------------------------------
# TestGetRomSaveInfo
# ---------------------------------------------------------------------------


class TestGetRomSaveInfo:
    """Tests for _get_rom_save_info."""

    def test_returns_info_for_installed_rom(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["system"] == "gba"
        assert result["rom_name"] == "pokemon"
        assert result["saves_dir"].endswith("saves/gba")

    def test_returns_none_for_missing_rom(self, tmp_path):
        svc, _ = make_service(tmp_path)

        result = svc._get_rom_save_info(999)

        assert result is None

    def test_returns_none_for_empty_system(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_name": "game.gba",
            "file_path": "/some/path.gba",
            "system": "",
            "platform_slug": "",
            "installed_at": "2026-01-01T00:00:00",
        }

        result = svc._get_rom_save_info(42)

        assert result is None

    # ------------------------------------------------------------------
    # Regression tests for issue #238 — Rule 1: when a save-sort migration
    # is pending, prefer save_sort_settings_previous so sync reads the
    # layout RetroArch actually wrote to during the session that just
    # ended.
    # ------------------------------------------------------------------

    def test_get_rom_save_info_prefers_previous_sort_settings_when_migration_pending(self, tmp_path):
        """Pending migration: previous (OLD) sort settings override current (NEW) (#238)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        # NEW layout (what settings currently say):
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}
        # OLD layout (what the session actually wrote to):
        svc._state["save_sort_settings_previous"] = {"sort_by_content": True, "sort_by_core": False}

        result = svc._get_rom_save_info(42)

        assert result is not None
        # OLD layout: no /mGBA subdir.
        assert result["saves_dir"].endswith("saves/gba")
        assert "/mGBA" not in result["saves_dir"]

    def test_get_rom_save_info_uses_current_sort_settings_when_no_pending_migration(self, tmp_path):
        """No pending migration: use current sort settings (#238)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        # Only save_sort_settings is present — no pending migration key at all.
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}
        assert "save_sort_settings_previous" not in svc._state

        result = svc._get_rom_save_info(42)

        assert result is not None
        # CURRENT layout: /mGBA subdir is appended because sort_by_core=True.
        assert result["saves_dir"].endswith("saves/gba/mGBA")

    def test_pending_sort_settings_rejects_empty_dict_half_state(self, tmp_path):
        """Empty-dict ``save_sort_settings_previous`` must NOT count as pending (#238 review).

        Freezes the contract: ``_get_rom_save_info`` and
        ``_is_save_sort_changed`` must agree on what counts as pending.
        Before ``_pending_sort_settings`` was introduced, a literal
        empty dict at ``save_sort_settings_previous`` would put the
        service in a half-state — ``_get_rom_save_info`` would fall
        back to current settings (``{} or current``), but
        ``_is_save_sort_changed`` would treat the same ``{}`` as
        pending (``is not None``). This test locks in the agreement.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        # Half-state input: empty previous, populated current (NEW).
        svc._state["save_sort_settings_previous"] = {}
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        # Both call sites must agree there is NO pending migration.
        assert svc._is_save_sort_changed() is False
        assert svc._pending_sort_settings() is None

        result = svc._get_rom_save_info(42)
        assert result is not None
        # Reads CURRENT settings (NEW layout), not the empty previous —
        # mGBA subdir is appended because sort_by_core=True.
        assert result["saves_dir"].endswith("saves/gba/mGBA")

    # ------------------------------------------------------------------
    # Regression tests for issue #232 — SaveService must resolve the
    # RetroArch ``corename`` via the .info parser when sort_by_core is
    # active, and must fall back with a warning when it cannot.
    # ------------------------------------------------------------------

    def test_default_sort_only_by_content_no_core_subdir(self, tmp_path):
        """sort_by_core=False (RetroDECK default) → no core subdir."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": False}

        result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        assert "/mGBA" not in result["saves_dir"]

    def test_sort_by_core_appends_retroarch_corename(self, tmp_path):
        """sort_by_core=True with resolvable corename → saves_dir ends in /{system}/{corename}."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba/mGBA")

    def test_sort_by_core_uses_corename_not_es_de_label(self, tmp_path):
        """The RetroArch .info corename (``Snes9x``) must be used, not the ES-DE label (``Snes9x - Current``)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x - Current"),
            get_core_name=lambda core_so: "Snes9x",
        )
        _install_rom(svc, tmp_path, system="snes", file_name="mario.sfc")
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/snes/Snes9x")
        assert "Snes9x - Current" not in result["saves_dir"]

    def test_sort_by_core_falls_back_when_corename_none(self, tmp_path, caplog):
        """sort_by_core=True but corename unresolvable → warn + fall back to parent dir.

        The warning must include ``core_so=mgba_libretro`` so a user can identify
        which ``.info`` file the parser failed on.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            get_core_name=lambda core_so: None,  # .info unreadable / field missing
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        with caplog.at_level("WARNING"):
            result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        assert "/mGBA" not in result["saves_dir"]
        warnings = [rec.message for rec in caplog.records if "unable to resolve RetroArch corename" in rec.message]
        assert warnings, "expected fallback warning"
        assert "core_so=mgba_libretro" in warnings[0]

    def test_sort_by_core_falls_back_when_get_core_name_missing(self, tmp_path, caplog):
        """Constructed without get_core_name → still warns and falls back.

        When ``get_core_name`` is not injected, the helper short-circuits before
        calling ``get_active_core``, so ``core_so`` is never resolved and the log
        records ``core_so=unresolved`` for that case.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
            # get_core_name intentionally omitted (defaults to None)
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        with caplog.at_level("WARNING"):
            result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        warnings = [rec.message for rec in caplog.records if "unable to resolve RetroArch corename" in rec.message]
        assert warnings, "expected fallback warning"
        assert "core_so=unresolved" in warnings[0]

    def test_sort_by_core_falls_back_when_active_core_unresolved(self, tmp_path, caplog):
        """sort_by_core=True but get_active_core returns (None, None) → warn + fall back.

        When ES-DE cannot determine the active core, ``core_so`` is ``None`` and
        the log records ``core_so=unresolved``.
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: (None, None),
            get_core_name=lambda core_so: "mGBA",
        )
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": True}

        with caplog.at_level("WARNING"):
            result = svc._get_rom_save_info(42)

        assert result is not None
        assert result["saves_dir"].endswith("saves/gba")
        warnings = [rec.message for rec in caplog.records if "unable to resolve RetroArch corename" in rec.message]
        assert warnings, "expected fallback warning"
        assert "core_so=unresolved" in warnings[0]

    def test_resolve_retroarch_corename_happy_path(self, tmp_path):
        """Direct test of the helper: both callbacks resolve → (corename, core_so) tuple returned."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x - Current"),
            get_core_name=lambda core_so: "Snes9x",
        )
        assert svc._resolve_retroarch_corename("snes", "mario.sfc") == ("Snes9x", "snes9x_libretro")

    def test_resolve_retroarch_corename_returns_none_tuple_when_core_so_empty(self, tmp_path):
        """ES-DE returns (None, None) → helper returns (None, None)."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: (None, None),
            get_core_name=lambda core_so: "Snes9x",
        )
        assert svc._resolve_retroarch_corename("snes", "mario.sfc") == (None, None)

    def test_resolve_retroarch_corename_preserves_core_so_when_corename_empty(self, tmp_path):
        """Empty corename with resolved core_so → (None, core_so).

        The core_so is preserved in the second element so the caller can log
        which ``.info`` file failed diagnostically. The first element is None
        because the empty-string corename is treated as "no usable value".
        """
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x"),
            get_core_name=lambda core_so: "",
        )
        assert svc._resolve_retroarch_corename("snes", "mario.sfc") == (None, "snes9x_libretro")


# ---------------------------------------------------------------------------
# TestUploadSpecialChars
# ---------------------------------------------------------------------------


class TestUploadSpecialChars:
    """Upload with special characters (spaces, parentheses) in filename."""

    def test_find_saves_with_special_chars(self, tmp_path):
        svc, _ = make_service(tmp_path)
        rom_name = "Metroid - Zero Mission (USA)"
        file_name = f"{rom_name}.gba"
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name=file_name)
        _create_save(tmp_path, system="gba", rom_name=rom_name)

        result = svc._find_save_files(42)

        assert len(result) == 1
        assert result[0]["filename"] == f"{rom_name}.srm"


# ---------------------------------------------------------------------------
# TestUpdateFileSyncState
# ---------------------------------------------------------------------------


class TestUpdateFileSyncState:
    """Tests for _update_file_sync_state."""

    def test_creates_proper_entry(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._update_file_sync_state("42", "pokemon.srm", server_resp, str(save_file), "gba")

        entry = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert entry["last_sync_hash"] == svc._file_md5(str(save_file))
        assert entry["last_sync_at"] is not None
        assert entry["last_sync_server_save_id"] == 200

    def test_creates_entry_with_new_fields(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._update_file_sync_state(
            "42",
            "pokemon.srm",
            server_resp,
            str(save_file),
            "gba",
            emulator_tag="retroarch-mgba",
            core_so="mgba_libretro",
        )

        game_state = svc._save_sync_state["saves"]["42"]
        assert game_state["emulator"] == "retroarch-mgba"
        assert game_state["last_synced_core"] == "mgba_libretro"
        assert game_state["active_slot"] == "default"

        file_state = game_state["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 200
        assert file_state["last_sync_server_save_id"] == 200

    def test_updates_emulator_on_existing_entry(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        # Pre-populate with old emulator tag
        svc._save_sync_state["saves"]["42"] = {
            "files": {},
            "emulator": "retroarch",
            "system": "gba",
            "last_synced_core": None,
            "active_slot": "default",
        }
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._update_file_sync_state(
            "42",
            "pokemon.srm",
            server_resp,
            str(save_file),
            "gba",
            emulator_tag="retroarch-mgba",
            core_so="mgba_libretro",
        )

        game_state = svc._save_sync_state["saves"]["42"]
        assert game_state["emulator"] == "retroarch-mgba"
        assert game_state["last_synced_core"] == "mgba_libretro"

    def test_core_so_none_does_not_overwrite(self, tmp_path):
        """core_so=None should not reset an already-set last_synced_core."""
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        svc._save_sync_state["saves"]["42"] = {
            "files": {},
            "emulator": "retroarch-mgba",
            "system": "gba",
            "last_synced_core": "mgba_libretro",
            "active_slot": "default",
        }
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._update_file_sync_state(
            "42",
            "pokemon.srm",
            server_resp,
            str(save_file),
            "gba",
            emulator_tag="retroarch",
        )

        # last_synced_core unchanged because core_so=None
        game_state = svc._save_sync_state["saves"]["42"]
        assert game_state["last_synced_core"] == "mgba_libretro"

    def test_writes_last_sync_local_mtime_as_float(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"\x00" * 1024)
        local_path = str(save_file)
        server_response = _server_save()

        svc._update_file_sync_state("42", "pokemon.srm", server_response, local_path, "gba")

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert isinstance(file_state["last_sync_local_mtime"], float)
        assert file_state["last_sync_local_mtime"] == pytest.approx(os.path.getmtime(local_path))

    def test_writes_last_sync_local_size_as_int(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"\x00" * 2048)
        local_path = str(save_file)
        server_response = _server_save()

        svc._update_file_sync_state("42", "pokemon.srm", server_response, local_path, "gba")

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert isinstance(file_state["last_sync_local_size"], int)
        assert file_state["last_sync_local_size"] == 2048

    def test_does_not_write_old_local_mtime_at_last_sync_key(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path, system="gba", rom_name="pokemon")
        local_path = str(save_file)
        server_response = _server_save()

        svc._update_file_sync_state("42", "pokemon.srm", server_response, local_path, "gba")

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert "local_mtime_at_last_sync" not in file_state

    def test_writes_none_for_missing_file(self, tmp_path):
        svc, _ = make_service(tmp_path)
        local_path = str(tmp_path / "saves" / "gba" / "missing.srm")
        server_response = _server_save()

        svc._update_file_sync_state("42", "missing.srm", server_response, local_path, "gba")

        file_state = svc._save_sync_state["saves"]["42"]["files"]["missing.srm"]
        assert file_state["last_sync_local_mtime"] is None
        assert file_state["last_sync_local_size"] is None


# ---------------------------------------------------------------------------
# TestPruneOrphanedEdgeCase
# ---------------------------------------------------------------------------


class TestPruneOrphanedEdgeCase:
    """Edge case for prune_orphaned_state not covered in TestStateManagement."""

    def test_empty_state_no_crash(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["saves"] = {}
        svc._save_sync_state["playtime"] = {}
        svc._state["shortcut_registry"] = {}

        svc.prune_orphaned_state()  # should not raise

        assert svc._save_sync_state["saves"] == {}
        assert svc._save_sync_state["playtime"] == {}


# ---------------------------------------------------------------------------
# TestStateBackwardCompat
# ---------------------------------------------------------------------------


class TestStateBackwardCompat:
    """Backward compat: old state files without new fields load and work."""

    def test_old_state_without_server_device_id_loads_fine(self, tmp_path):
        """Existing state files without server_device_id should load without errors."""
        svc, _ = make_service(tmp_path)
        # Simulate old state without server_device_id
        svc._save_sync_state["device_id"] = "old-local-uuid"
        svc._save_sync_state["saves"]["42"] = {
            "files": {"pokemon.srm": {"last_sync_hash": "abc123"}},
            "emulator": "retroarch",
            "system": "gba",
        }
        # Remove the new field to simulate an old state file
        del svc._save_sync_state["server_device_id"]
        svc.save_state()

        # Reload into fresh service
        svc2, _ = make_service(tmp_path)
        svc2.load_state()

        # New field should be None (from init_state default)
        assert svc2._save_sync_state.get("server_device_id") is None
        # Old data preserved
        assert svc2._save_sync_state["device_id"] == "old-local-uuid"
        assert "42" in svc2._save_sync_state["saves"]

    def test_old_per_game_entry_missing_new_fields_works_via_get(self, tmp_path):
        """Per-game entries without last_synced_core/active_slot still work via .get()."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["device_id"] = "old-local-uuid"
        svc._save_sync_state["saves"]["42"] = {
            "files": {"pokemon.srm": {"last_sync_hash": "abc123"}},
            "emulator": "retroarch",
            "system": "gba",
        }
        svc.save_state()

        svc2, _ = make_service(tmp_path)
        svc2.load_state()

        game_state = svc2._save_sync_state["saves"]["42"]
        assert game_state.get("last_synced_core") is None
        assert game_state.get("active_slot", "default") == "default"

    def test_make_default_state_includes_server_device_id(self):
        """make_default_state() must include server_device_id field."""
        state = SaveService.make_default_state()
        assert "server_device_id" in state
        assert state["server_device_id"] is None

    def test_load_state_restores_server_device_id(self, tmp_path):
        """server_device_id saved to disk is restored on load_state."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["server_device_id"] = "romm-server-uuid"
        svc.save_state()

        svc2, _ = make_service(tmp_path)
        svc2.load_state()
        assert svc2._save_sync_state["server_device_id"] == "romm-server-uuid"

    def test_state_stores_emulator_tag_and_core(self, tmp_path):
        """After upload sync, state should contain emulator tag and core info."""
        svc, _fake = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("mgba_libretro", "mGBA"),
        )
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        svc._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")

        game_state = svc._save_sync_state["saves"]["42"]
        assert game_state["emulator"] == "retroarch-mgba"
        assert game_state["last_synced_core"] == "mgba_libretro"
        assert game_state.get("active_slot") == "default"

        # Per-file should have tracked_save_id
        file_state = game_state["files"]["pokemon.srm"]
        assert file_state.get("tracked_save_id") is not None

    def test_download_sets_tracked_save_id_in_file_state(self, tmp_path):
        """After download sync, per-file state should contain tracked_save_id."""
        svc, _ = make_service(tmp_path)
        saves_dir = str(tmp_path / "saves" / "gba")
        os.makedirs(saves_dir, exist_ok=True)
        server_save = _server_save(save_id=99)

        svc._do_download_save(server_save, saves_dir, "pokemon.srm", "42", "gba")

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state.get("tracked_save_id") == 99
        assert file_state.get("last_sync_server_save_id") == 99


# ---------------------------------------------------------------------------
# TestV47SyncFlow
# ---------------------------------------------------------------------------


class TestV47SyncFlow:
    def test_list_saves_passes_device_id(self, tmp_path):
        """v4.7: list_saves receives server_device_id."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "local-id"
        svc._save_sync_state["server_device_id"] = "server-dev-123"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        svc._sync_rom_saves(42)

        list_calls = [c for c in fake.call_log if c[0] == "list_saves"]
        assert len(list_calls) >= 1
        assert list_calls[0][2]["device_id"] == "server-dev-123"

    def test_upload_passes_device_id_and_slot(self, tmp_path):
        """v4.7: upload_save receives device_id and slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "local-id"
        svc._save_sync_state["server_device_id"] = "server-dev-123"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        svc._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        assert upload_calls[0][2]["device_id"] == "server-dev-123"
        assert upload_calls[0][2]["slot"] == "default"

    def test_v47_skip_when_is_current(self, tmp_path):
        """v4.7: server says is_current=True, local unchanged → skip."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        content = b"same content"
        _create_save(tmp_path, content=content)
        local_hash = hashlib.md5(content).hexdigest()

        # Pre-populate sync state (simulating previous sync)
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_save_id": 100,
                    "last_sync_server_size": len(content),
                }
            }
        }

        # Set up server save with device_syncs showing is_current=True
        fake.saves[100] = {
            "id": 100,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2026-02-17T06:00:00Z",
            "file_size_bytes": len(content),
            "device_syncs": [{"device_id": "dev-1", "is_current": True}],
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)
        assert synced == 0
        assert errors == []
        assert conflicts == []

    def test_v47_download_when_not_current(self, tmp_path):
        """v4.7: server says is_current=False, local unchanged → download."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        content = b"old content"
        _create_save(tmp_path, content=content)
        local_hash = hashlib.md5(content).hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_save_id": 100,
                    "last_sync_server_size": len(content),
                }
            }
        }

        # Server has newer save, device is not current
        fake.saves[100] = {
            "id": 100,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2026-02-17T08:00:00Z",
            "file_size_bytes": 2048,
            "device_syncs": [{"device_id": "dev-1", "is_current": False}],
        }

        synced, errors, _conflicts = svc._sync_rom_saves(42)
        assert synced == 1
        assert errors == []
        # Verify download happened
        assert 100 in fake.downloaded_files


# ---------------------------------------------------------------------------
# TestConfirmDownloadAfterSync
# ---------------------------------------------------------------------------


class TestConfirmDownloadAfterSync:
    """Verify the device's last_synced_at is registered with RomM after each
    upload (PUT/POST) and download.

    is_current is computed server-side as
    ``device_save_sync.last_synced_at >= save.updated_at``. PUT/POST bump
    ``save.updated_at`` to NOW but do NOT touch the calling device's
    ``last_synced_at`` in every code path; we explicitly close that gap by
    calling ``confirm_download``. For downloads, the optimistic query-param on
    ``download_save_content`` upserts the row server-side before streaming.
    """

    def test_do_upload_save_post_calls_confirm_download(self, tmp_path):
        """POST (no save_id) → confirm_download fires for the new save_id."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        svc._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # FakeSaveApi mints a new save_id starting from 1000 on POST
        new_save_id = next(iter(fake.saves.values()))["id"]

        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert len(confirm_calls) == 1
        assert confirm_calls[0][1] == (new_save_id, "dev-1")

    def test_do_upload_save_put_calls_confirm_download(self, tmp_path):
        """PUT (existing save_id) → confirm_download fires for that save_id."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # Pre-existing tracked server save
        fake.saves[100] = _server_save(save_id=100, rom_id=42)
        server_save = fake.saves[100]

        svc._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba", server_save=server_save)

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT path: save_id kwarg passed to upload_save
        assert upload_calls[0][2]["save_id"] == 100

        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert len(confirm_calls) == 1
        assert confirm_calls[0][1] == (100, "dev-1")

    def test_do_upload_save_skips_confirm_when_no_device_id(self, tmp_path):
        """No registered device → confirm_download is not called (no-op)."""
        svc, fake = make_service(tmp_path)
        # server_device_id stays None — device not registered
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        svc._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")

        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert confirm_calls == []

    def test_do_upload_save_swallows_confirm_download_error(self, tmp_path):
        """confirm_download failure must NOT bubble — upload is reported successful."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # Patch confirm_download to raise; the upload itself must still complete.
        original_confirm = fake.confirm_download

        def boom(save_id: int, device_id: str) -> dict:
            fake.call_log.append(("confirm_download", (save_id, device_id), {}))
            raise RommApiError("HTTP 500: Server Error", url="/api/saves/x/downloaded", method="POST")

        fake.confirm_download = boom  # type: ignore[method-assign]
        try:
            result = svc._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")
        finally:
            fake.confirm_download = original_confirm  # type: ignore[method-assign]

        # Upload completed, returned a result with id, AND the file_state was updated.
        assert result.get("id") is not None
        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert len(confirm_calls) == 1
        # File state still recorded the upload (not blocked by confirm failure)
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state.get("tracked_save_id") is not None

    def test_do_download_save_passes_device_id_and_optimistic(self, tmp_path):
        """download_save_content must pass device_id + optimistic=True so the
        server upserts our DeviceSaveSync row before streaming. This makes a
        follow-up confirm_download unnecessary for the download path.
        """
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["server_device_id"] = "dev-1"
        saves_dir = str(tmp_path / "saves" / "gba")
        os.makedirs(saves_dir, exist_ok=True)
        server_save = _server_save(save_id=99)

        svc._do_download_save(server_save, saves_dir, "pokemon.srm", "42", "gba")

        dl_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(dl_calls) == 1
        kwargs = dl_calls[0][2]
        assert kwargs["device_id"] == "dev-1"
        assert kwargs["optimistic"] is True


class TestSaveSyncSettingsSlotAndCleanup:
    """Tests for default_slot and autocleanup_limit settings."""

    def test_update_default_slot(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        result = svc.update_save_sync_settings({"default_slot": "desktop"})
        assert result["success"] is True
        assert result["settings"]["default_slot"] == "desktop"

    def test_update_default_slot_empty_string_becomes_none(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["settings"]["default_slot"] = "default"
        result = svc.update_save_sync_settings({"default_slot": ""})
        assert result["settings"]["default_slot"] is None

    def test_empty_string_becomes_none(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", "")
        assert val is None
        assert skip is False

    def test_none_value_passes_through(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", None)
        assert val is None
        assert skip is False

    def test_whitespace_only_becomes_none(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", "   ")
        assert val is None
        assert skip is False

    def test_nonempty_string_trimmed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", "  desktop  ")
        assert val == "desktop"
        assert skip is False

    def test_upload_uses_none_slot_when_active_slot_is_none(self, tmp_path):
        """When active_slot key is present but value is None, .get() returns None (legacy mode)."""
        _svc, _ = make_service(tmp_path)
        game_state: dict = {"active_slot": None}
        slot = game_state.get("active_slot", "default")
        assert slot is None

    def test_update_autocleanup_limit(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        result = svc.update_save_sync_settings({"autocleanup_limit": 5})
        assert result["success"] is True
        assert result["settings"]["autocleanup_limit"] == 5

    def test_update_autocleanup_limit_clamped(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        result = svc.update_save_sync_settings({"autocleanup_limit": 0})
        assert result["settings"]["autocleanup_limit"] == 1

    def test_get_settings_includes_new_defaults(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.get_save_sync_settings()
        assert result["default_slot"] == "default"
        assert result["autocleanup_limit"] == 10


class TestSaveSlots:
    """Tests for get_save_slots and _set_active_slot."""

    @pytest.mark.asyncio
    async def test_get_save_slots(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "server-dev-1"

        fake.saves[1] = {
            "id": 1,
            "rom_id": 123,
            "file_name": "a.srm",
            "updated_at": "2026-03-24T10:00:00",
            "slot": "default",
        }
        fake.saves[2] = {
            "id": 2,
            "rom_id": 123,
            "file_name": "b.srm",
            "updated_at": "2026-03-24T08:00:00",
            "slot": "desktop",
        }

        result = await svc.get_save_slots(123)
        assert result["success"] is True
        assert len(result["slots"]) == 2
        assert result["active_slot"] == "default"

    @pytest.mark.asyncio
    async def test_get_save_slots_latest_updated_at_from_server(self, tmp_path):
        """latest_updated_at is populated from nested latest.updated_at, not a flat key."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "server-dev-1"

        # Two saves in the default slot; the later one should win.
        fake.saves[1] = {
            "id": 1,
            "rom_id": 123,
            "file_name": "a.srm",
            "updated_at": "2026-04-16T13:00:00",
            "slot": "default",
        }
        fake.saves[2] = {
            "id": 2,
            "rom_id": 123,
            "file_name": "b.srm",
            "updated_at": "2026-04-17T20:00:00",
            "slot": "default",
        }

        result = await svc.get_save_slots(123)
        assert result["success"] is True
        slot = next(s for s in result["slots"] if s["slot"] == "default")
        assert slot["latest_updated_at"] == "2026-04-17T20:00:00"

        # Also verify the value is persisted in state (not None)
        persisted = svc._save_sync_state["saves"]["123"]["slots"]["default"]
        assert persisted["latest_updated_at"] == "2026-04-17T20:00:00"

    @pytest.mark.asyncio
    async def test_get_save_slots_disabled(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.get_save_slots(123)
        assert result["success"] is False

    def test_set_active_slot(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["saves"] = {
            "123": {"system": "gba", "active_slot": "default", "files": {}},
        }
        result = svc._slots._set_active_slot(123, "desktop")
        assert result["success"] is True
        assert svc._save_sync_state["saves"]["123"]["active_slot"] == "desktop"

    def test_set_active_slot_creates_entry(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        result = svc._slots._set_active_slot(456, "my-slot")
        assert result["success"] is True
        assert svc._save_sync_state["saves"]["456"]["active_slot"] == "my-slot"

    def test_set_active_slot_empty_sets_none(self, tmp_path):
        """Empty string sets active_slot to None (legacy mode)."""
        svc, _ = make_service(tmp_path)
        result = svc._slots._set_active_slot(123, "")
        assert result["success"] is True
        assert result["active_slot"] is None
        assert svc._save_sync_state["saves"]["123"]["active_slot"] is None

    @pytest.mark.asyncio
    async def test_set_active_slot_triggers_background_check(self, tmp_path):
        """_set_active_slot fires a background save status check task."""
        emitted = []

        async def fake_emit(event, *args):
            emitted.append((event, args))

        svc, _ = make_service(tmp_path, emit=fake_emit)
        _install_rom(svc, tmp_path)

        svc._slots._set_active_slot(42, "slot1")

        # Give the background task a chance to run
        await asyncio.sleep(0.1)

        assert any(e[0] == "save_status_updated" for e in emitted)


# ---------------------------------------------------------------------------
# TestSaveTrackingConfigured
# ---------------------------------------------------------------------------


class TestSaveTrackingConfigured:
    def test_not_configured_by_default(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.is_save_tracking_configured(42)
        assert result["configured"] is False
        assert result["active_slot"] is None

    def test_configured_after_setting_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["saves"]["42"] = {
            "slot_confirmed": True,
            "active_slot": "default",
            "files": {},
        }
        result = svc.is_save_tracking_configured(42)
        assert result["configured"] is True
        assert result["active_slot"] == "default"

    def test_not_configured_when_slot_confirmed_false(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["saves"]["42"] = {
            "slot_confirmed": False,
            "active_slot": "default",
            "files": {},
        }
        result = svc.is_save_tracking_configured(42)
        assert result["configured"] is False
        assert result["active_slot"] is None

    def test_handles_missing_saves_section(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["saves"] = {}
        result = svc.is_save_tracking_configured(999)
        assert result["configured"] is False


# ---------------------------------------------------------------------------
# TestGetSaveSetupInfo
# ---------------------------------------------------------------------------


class TestGetSaveSetupInfo:
    @pytest.mark.asyncio
    async def test_scenario_a_no_local_server_has_saves(self, tmp_path):
        """Scenario A: No local save, server has saves."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        # Don't create local save
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is False
        assert len(result["local_files"]) == 0
        assert len(result["server_slots"]) == 1
        assert result["server_slots"][0]["slot"] is None
        assert result["server_slots"][0]["count"] == 1
        assert result["slot_confirmed"] is False
        assert result["active_slot"] is None

    @pytest.mark.asyncio
    async def test_scenario_b_local_no_server(self, tmp_path):
        """Scenario B: Local save exists, no server saves."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert len(result["local_files"]) == 1
        assert result["local_files"][0]["filename"] == "pokemon.srm"
        assert len(result["server_slots"]) == 0
        assert result["slot_confirmed"] is False

    @pytest.mark.asyncio
    async def test_scenario_c_local_and_server_different_slots(self, tmp_path):
        """Scenario C: Local save, server has saves in different slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot="desktop")

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert len(result["server_slots"]) == 1
        assert result["server_slots"][0]["slot"] == "desktop"
        assert result["default_slot"] == "default"

    @pytest.mark.asyncio
    async def test_scenario_e_local_and_server_same_default_slot(self, tmp_path):
        """Scenario E: Local save, server has saves in default slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot="default")

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert len(result["server_slots"]) == 1
        assert result["server_slots"][0]["slot"] == "default"
        assert result["default_slot"] == "default"

    @pytest.mark.asyncio
    async def test_already_confirmed(self, tmp_path):
        """When slot is already confirmed, report it."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["saves"]["42"] = {
            "slot_confirmed": True,
            "active_slot": "desktop",
            "files": {},
        }
        _install_rom(svc, tmp_path)

        result = await svc.get_save_setup_info(42)
        assert result["slot_confirmed"] is True
        assert result["active_slot"] == "desktop"

    @pytest.mark.asyncio
    async def test_multiple_server_slots(self, tmp_path):
        """Server saves across multiple slots are grouped correctly."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot="default")
        fake.saves[2] = _server_save(save_id=2, slot="desktop", filename="pokemon.srm")

        result = await svc.get_save_setup_info(42)
        assert len(result["server_slots"]) == 2
        slot_names = {s["slot"] for s in result["server_slots"]}
        assert slot_names == {"default", "desktop"}

    @pytest.mark.asyncio
    async def test_server_error_returns_empty_slots(self, tmp_path):
        """Server API failure still returns local info with empty server_slots."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.fail_on_next(RommApiError(500, "Server error"))

        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is True
        assert result["server_slots"] == []

    @pytest.mark.asyncio
    async def test_no_rom_installed(self, tmp_path):
        """No installed ROM means no local files."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        # Don't install any ROM
        result = await svc.get_save_setup_info(42)
        assert result["has_local_saves"] is False
        assert result["local_files"] == []


# ---------------------------------------------------------------------------
# TestConfirmSlotChoice
# ---------------------------------------------------------------------------


class TestConfirmSlotChoice:
    @pytest.mark.asyncio
    async def test_confirm_sets_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        result = await svc.confirm_slot_choice(42, "default")
        assert result["success"] is True
        state = svc._save_sync_state["saves"]["42"]
        assert state["slot_confirmed"] is True
        assert state["active_slot"] == "default"

    @pytest.mark.asyncio
    async def test_confirm_empty_slot_rejected(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.confirm_slot_choice(42, "")
        assert result["success"] is False
        assert "empty" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_confirm_whitespace_slot_rejected(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.confirm_slot_choice(42, "   ")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_confirm_preserves_existing_files_state(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["saves"]["42"] = {
            "files": {"pokemon.srm": {"last_sync_hash": "abc"}},
            "active_slot": "old",
        }
        result = await svc.confirm_slot_choice(42, "new-slot")
        assert result["success"] is True
        state = svc._save_sync_state["saves"]["42"]
        assert state["active_slot"] == "new-slot"
        assert state["slot_confirmed"] is True
        # Existing files state preserved
        assert state["files"]["pokemon.srm"]["last_sync_hash"] == "abc"

    @pytest.mark.asyncio
    async def test_confirm_persists_to_disk(self, tmp_path):
        svc, _ = make_service(tmp_path)
        await svc.confirm_slot_choice(42, "default")
        # State file should exist
        import json

        state_path = tmp_path / "save_sync_state.json"
        assert state_path.exists()
        saved = json.loads(state_path.read_text())
        assert saved["saves"]["42"]["slot_confirmed"] is True

    @pytest.mark.asyncio
    async def test_confirm_with_legacy_no_slot_migration(self, tmp_path):
        """Migrate: re-upload to new slot, delete old.

        ``None`` for ``migrate_from_slot`` means "migrate from legacy
        no-slot server saves". Facade translates ``None`` to the
        no-migration sentinel, so this exercises ``SlotsService`` directly
        where the legacy ``None`` semantics still live.
        """
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        # Old save on server with slot=None (legacy)
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc._slots.confirm_slot_choice(42, "default", migrate_from_slot=None)
        assert result["success"] is True
        # New save should have been uploaded
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) >= 1
        # Check it was uploaded with the new slot
        assert upload_calls[0][2].get("slot") == "default"
        # Old save should have been deleted
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 1
        assert 1 in delete_calls[0][1][0]  # save_id 1 in the list

    @pytest.mark.asyncio
    async def test_confirm_migration_no_old_saves(self, tmp_path):
        """Migration with no matching old saves is a no-op."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        # Server save is in "default" slot, but we're migrating from "desktop"
        fake.saves[1] = _server_save(save_id=1, slot="default")

        result = await svc.confirm_slot_choice(42, "default", migrate_from_slot="desktop")
        assert result["success"] is True
        # No upload or delete should happen (no saves in "desktop" slot)
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 0
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 0

    @pytest.mark.asyncio
    async def test_confirm_migration_failure_still_confirms_slot(self, tmp_path):
        """Migration failure should still confirm the slot but report the issue.

        Exercises ``SlotsService`` directly because the facade translates
        ``None`` to the no-migration sentinel; legacy ``None`` migration
        semantics live on the slots service.
        """
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot=None)

        # Make upload_save fail during migration
        def failing_upload(*args, **kwargs):
            raise RommApiError(500, "Server error")

        fake.upload_save = failing_upload

        result = await svc._slots.confirm_slot_choice(42, "default", migrate_from_slot=None)
        assert result["success"] is True
        assert "migration failed" in result["message"].lower()
        # Slot is still confirmed despite migration failure
        assert svc._save_sync_state["saves"]["42"]["slot_confirmed"] is True

    @pytest.mark.asyncio
    async def test_facade_translates_none_to_no_migration(self, tmp_path):
        """Facade: ``None`` for ``migrate_from_slot`` skips migration."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc.confirm_slot_choice(42, "default", migrate_from_slot=None)
        assert result["success"] is True
        # No migration occurred — no uploads / no deletes
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 0
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 0
        assert svc._save_sync_state["saves"]["42"]["slot_confirmed"] is True

    @pytest.mark.asyncio
    async def test_facade_translates_no_migration_string_to_no_migration(self, tmp_path):
        """Facade: ``"__no_migration__"`` string (from frontend) skips migration."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc.confirm_slot_choice(42, "default", migrate_from_slot="__no_migration__")
        assert result["success"] is True
        # No migration occurred — no uploads / no deletes
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 0
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 0
        assert svc._save_sync_state["saves"]["42"]["slot_confirmed"] is True

    @pytest.mark.asyncio
    async def test_is_configured_after_confirm(self, tmp_path):
        """is_save_tracking_configured returns True after confirm_slot_choice."""
        svc, _ = make_service(tmp_path)
        assert svc.is_save_tracking_configured(42)["configured"] is False
        await svc.confirm_slot_choice(42, "default")
        result = svc.is_save_tracking_configured(42)
        assert result["configured"] is True
        assert result["active_slot"] == "default"


# ---------------------------------------------------------------------------
# TestTrackedSaveIdMatching
# ---------------------------------------------------------------------------


class TestTrackedSaveIdMatching:
    """Tests that sync uses tracked_save_id to match server saves instead of filename."""

    def test_timestamp_server_save_not_treated_as_separate_download(self, tmp_path):
        """Server save matched by tracked_save_id should not appear as server-only download."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        fake.saves[42] = {
            "id": 42,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-24_15-18-50].srm",
            "updated_at": "2026-03-20T10:00:00",
            "file_size_bytes": 1024,
            "emulator": "retroarch",
            "download_path": "/saves/pokemon [2026-03-24_15-18-50].srm",
        }

        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 42,
                    "last_sync_hash": local_hash,
                    "last_sync_at": "2026-03-20T10:00:00",
                    "last_sync_server_updated_at": "2026-03-20T10:00:00",
                    "last_sync_server_save_id": 42,
                    "last_sync_server_size": 1024,
                    "local_mtime_at_last_sync": "2026-03-20T10:00:00",
                },
            },
        }

        # Sync should NOT download the timestamp-named file as a new server-only save
        _synced, errors, _conflicts = svc._sync_rom_saves(42)
        assert len(errors) == 0
        # No downloads should have occurred (files are in sync)
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0

    @pytest.mark.asyncio
    async def test_get_save_status_uses_tracked_save_id(self, tmp_path):
        """get_save_status should not show timestamp-named server save as separate file."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        fake.saves[42] = {
            "id": 42,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-24_15-18-50].srm",
            "updated_at": "2026-03-20T10:00:00",
            "file_size_bytes": 1024,
            "emulator": "retroarch",
            "download_path": "/saves/pokemon [2026-03-24_15-18-50].srm",
        }

        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 42,
                    "last_sync_hash": hashlib.md5(b"\x00" * 1024).hexdigest(),
                    "last_sync_at": "2026-03-20T10:00:00",
                    "last_sync_server_updated_at": "2026-03-20T10:00:00",
                    "last_sync_server_save_id": 42,
                    "last_sync_server_size": 1024,
                    "local_mtime_at_last_sync": "2026-03-20T10:00:00",
                },
            },
        }

        result = await svc.get_save_status(42)
        filenames = [f["filename"] for f in result["files"]]
        # The timestamp-named server save should NOT appear as a separate file
        assert "pokemon [2026-03-24_15-18-50].srm" not in filenames
        # The local filename should appear
        assert "pokemon.srm" in filenames

    @pytest.mark.asyncio
    async def test_status_fallback_matches_newest_no_phantom_downloads(self, tmp_path):
        """Status with no tracked_save_id matches newest server save, no phantom downloads."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        fake.saves[10] = {
            "id": 10,
            "rom_id": 42,
            "file_name": "pokemon [old].srm",
            "updated_at": "2026-03-24T10:00:00",
            "file_size_bytes": 100,
            "emulator": "retroarch",
            "download_path": "/saves/pokemon [old].srm",
            "slot": "default",
        }
        fake.saves[20] = {
            "id": 20,
            "rom_id": 42,
            "file_name": "pokemon [new].srm",
            "updated_at": "2026-03-24T15:00:00",
            "file_size_bytes": 200,
            "emulator": "retroarch",
            "download_path": "/saves/pokemon [new].srm",
            "slot": "default",
        }

        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {},
        }

        result = await svc.get_save_status(42)
        filenames = [f["filename"] for f in result["files"]]

        # The local file should appear (matched to newest server save)
        assert "pokemon.srm" in filenames
        # Timestamp server files should NOT appear as separate entries
        assert "pokemon [old].srm" not in filenames
        assert "pokemon [new].srm" not in filenames

    def test_server_only_downloads_newest_with_local_filename(self, tmp_path):
        """Case 2: no local file, server has multiple timestamped saves.
        Should download only the newest, saved as the correct local filename."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        # NO local save created — Case 2

        # Server has 3 timestamped versions of the same save
        for sid, ts in [(16, "15-18-50"), (17, "15-19-15"), (18, "15-19-26")]:
            fake.saves[sid] = {
                "id": sid,
                "rom_id": 42,
                "file_name": f"pokemon [2026-03-24_{ts}].srm",
                "file_name_no_tags": "pokemon",
                "file_extension": "srm",
                "updated_at": f"2026-03-24T{ts.replace('-', ':')}",
                "file_size_bytes": 1024,
                "emulator": "retroarch-mgba",
                "slot": "default",
                "download_path": f"/saves/pokemon [2026-03-24_{ts}].srm",
            }

        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {},
        }

        synced, errors, _conflicts = svc._sync_rom_saves(42)
        assert len(errors) == 0
        assert synced == 1  # only ONE download

        # Should download only once (the newest, id=18)
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 1
        assert download_calls[0][1][0] == 18  # save_id=18 (newest)

        # File should be saved as pokemon.srm (local name), NOT timestamp name
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()
        assert not (saves_dir / "pokemon [2026-03-24_15-19-26].srm").exists()

    @pytest.mark.asyncio
    async def test_status_server_only_shows_local_filename(self, tmp_path):
        """Status display should show local filename for server-only saves, not timestamp."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        # NO local save

        fake.saves[18] = {
            "id": 18,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-24_15-19-26].srm",
            "file_name_no_tags": "pokemon",
            "file_extension": "srm",
            "updated_at": "2026-03-24T15:19:26",
            "file_size_bytes": 1024,
            "emulator": "retroarch-mgba",
            "slot": "default",
            "download_path": "/saves/pokemon [2026-03-24_15-19-26].srm",
        }

        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {},
        }

        result = await svc.get_save_status(42)
        filenames = [f["filename"] for f in result["files"]]
        assert "pokemon.srm" in filenames
        assert "pokemon [2026-03-24_15-19-26].srm" not in filenames


class TestOlderVersionSkipping:
    """Older stacked versions in the same slot must not be downloaded."""

    def test_different_slot_filtered_out(self, tmp_path):
        """Saves in a different slot should be filtered out entirely."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local save")

        # Matched in slot=default
        fake.saves[10] = _server_save(
            save_id=10,
            filename="pokemon.srm",
            updated_at="2026-03-24T15:00:00",
            slot="default",
        )
        # Unmatched in slot=portable — filtered out by active_slot
        fake.saves[20] = _server_save(
            save_id=20,
            filename="pokemon [old].srm",
            updated_at="2026-03-20T10:00:00",
            slot="portable",
        )

        local_hash = _file_md5(tmp_path / "saves" / "gba" / "pokemon.srm")
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 10,
                    "last_sync_hash": local_hash,
                    "last_sync_at": "2026-03-24T15:00:00",
                    "last_sync_server_updated_at": "2026-03-24T15:00:00",
                    "last_sync_server_save_id": 10,
                    "last_sync_server_size": 1024,
                    "local_mtime_at_last_sync": "2026-03-24T15:00:00",
                },
            },
        }

        _synced, _errors, _conflicts = svc._sync_rom_saves(42)
        # pokemon [old].srm in slot=portable is filtered out — no download
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0


# ---------------------------------------------------------------------------
# TestCheckCoreChange
# ---------------------------------------------------------------------------


class TestCheckCoreChange:
    """Tests for SaveService.check_core_change."""

    def _make_save_entry(
        self,
        system="snes",
        last_synced_core: str | None = "snes9x_libretro",
        active_slot="default",
    ):
        """Return a minimal save state entry for rom_id 42."""
        return {
            "system": system,
            "last_synced_core": last_synced_core,
            "active_slot": active_slot,
            "files": {},
        }

    def test_core_changed(self, tmp_path):
        """Returns changed=True with core names when active core differs from stored."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("supafaust_libretro", "Supafaust"),
        )
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["saves"]["42"] = self._make_save_entry(
            system="snes",
            last_synced_core="snes9x_libretro",
        )

        result = svc.check_core_change(42)

        assert result["changed"] is True
        assert result["old_core"] == "snes9x_libretro"
        assert result["new_core"] == "supafaust_libretro"
        assert result["old_label"] == "snes9x"
        assert result["new_label"] == "Supafaust"

    def test_core_same(self, tmp_path):
        """Returns changed=False when active core matches stored core."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x"),
        )
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["saves"]["42"] = self._make_save_entry(
            system="snes",
            last_synced_core="snes9x_libretro",
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_never_synced(self, tmp_path):
        """Returns changed=False when rom_id has no save entry (never synced)."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        # No entry for rom_id 42

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_no_stored_core(self, tmp_path):
        """Returns changed=False when save entry exists but last_synced_core is None."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("snes9x_libretro", "Snes9x"),
        )
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["saves"]["42"] = self._make_save_entry(
            system="snes",
            last_synced_core=None,
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_active_core_resolution_fails(self, tmp_path):
        """Returns changed=False when get_active_core returns (None, None)."""
        svc, _ = make_service(
            tmp_path,
            # default: get_active_core returns (None, None)
        )
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["saves"]["42"] = self._make_save_entry(
            system="snes",
            last_synced_core="snes9x_libretro",
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_save_sync_disabled(self, tmp_path):
        """Returns changed=False when save sync is disabled regardless of state."""
        svc, _ = make_service(
            tmp_path,
            get_active_core=lambda system_name, rom_filename=None: ("supafaust_libretro", "Supafaust"),
        )
        # save_sync_enabled defaults to False
        svc._save_sync_state["saves"]["42"] = self._make_save_entry(
            system="snes",
            last_synced_core="snes9x_libretro",
        )

        result = svc.check_core_change(42)

        assert result == {"changed": False}

    def test_rom_filename_resolved_for_per_game_core(self, tmp_path):
        """When installed_roms has file_path, the basename is passed to get_active_core."""
        received_args: list = []

        def capture_core(system_name, rom_filename=None):
            received_args.append((system_name, rom_filename))
            return ("supafaust_libretro", "Supafaust")

        svc, _ = make_service(tmp_path, get_active_core=capture_core)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["saves"]["42"] = self._make_save_entry(system="snes")
        _install_rom(svc, tmp_path, rom_id=42, system="snes", file_name="mario.sfc")

        svc.check_core_change(42)

        assert len(received_args) == 1
        assert received_args[0] == ("snes", "mario.sfc")


# ---------------------------------------------------------------------------
# TestGetSlotSaves
# ---------------------------------------------------------------------------


class TestGetSlotSaves:
    """Tests for get_slot_saves — lightweight server save listing by slot."""

    @pytest.mark.asyncio
    async def test_happy_path(self, tmp_path):
        """Returns mapped save dicts for the requested slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["server_device_id"] = "server-dev-1"

        fake.saves[1] = {
            "id": 1,
            "rom_id": 42,
            "file_name": "mario.srm",
            "updated_at": "2026-03-24T10:00:00Z",
            "file_size_bytes": 2048,
            "emulator": "retroarch",
            "slot": "default",
        }
        fake.saves[2] = {
            "id": 2,
            "rom_id": 42,
            "file_name": "mario.state",
            "updated_at": "2026-03-24T09:00:00Z",
            "file_size_bytes": 512,
            "emulator": "retroarch",
            "slot": "default",
        }

        result = await svc.get_slot_saves(42, "default")

        assert result["success"] is True
        assert result["slot"] == "default"
        assert len(result["saves"]) == 2
        save = next(s for s in result["saves"] if s["id"] == 1)
        assert save["filename"] == "mario.srm"
        assert save["size"] == 2048
        assert save["updated_at"] == "2026-03-24T10:00:00Z"
        assert save["emulator"] == "retroarch"
        # Verify list_saves was called with the correct slot kwarg
        assert any(call[0] == "list_saves" and call[2].get("slot") == "default" for call in fake.call_log)

    @pytest.mark.asyncio
    async def test_empty_slot(self, tmp_path):
        """Returns empty saves list when server has no saves for the slot."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["server_device_id"] = "server-dev-1"
        # No saves added to fake

        result = await svc.get_slot_saves(42, "desktop")

        assert result["success"] is True
        assert result["slot"] == "desktop"
        assert result["saves"] == []

    @pytest.mark.asyncio
    async def test_server_error(self, tmp_path):
        """Returns error response when list_saves raises an exception."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["server_device_id"] = "server-dev-1"
        fake.fail_on_next(RommApiError("connection timeout"))

        result = await svc.get_slot_saves(42, "default")

        assert result["success"] is False
        assert result["slot"] == "default"
        assert result["saves"] == []
        assert "connection timeout" in result["error"]

    @pytest.mark.asyncio
    async def test_sync_disabled(self, tmp_path):
        """Returns error response when save sync is disabled."""
        svc, _ = make_service(tmp_path)
        # save_sync_enabled defaults to False

        result = await svc.get_slot_saves(42, "default")

        assert result["success"] is False
        assert result["slot"] == "default"
        assert result["saves"] == []
        assert "disabled" in result["error"].lower()


# ---------------------------------------------------------------------------
# TestSwitchSlot
# ---------------------------------------------------------------------------


class TestSwitchSlot:
    """Tests for SaveService.switch_slot — guarded slot switch with immediate download."""

    def _synced_state(self, local_hash: str, save_id: int = 100) -> dict:
        """Return a save state dict where the file appears fully synced."""
        return {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_at": "2026-01-01T00:00:00Z",
                    "last_sync_server_updated_at": "2026-01-01T00:00:00Z",
                    "last_sync_server_save_id": save_id,
                    "last_sync_server_size": 1024,
                    "tracked_save_id": save_id,
                },
            },
            "active_slot": "default",
            "slot_confirmed": True,
        }

    @pytest.mark.asyncio
    async def test_happy_path(self, tmp_path):
        """Files fully synced + server has saves in new slot → downloads and returns success."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Slot already synced — hash matches
        svc._save_sync_state["saves"]["42"] = self._synced_state(local_hash)

        # Server has a save in "desktop" slot
        fake.saves[200] = _server_save(save_id=200, slot="desktop")

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is True
        assert "save_status" in result
        # active_slot was updated
        assert svc._save_sync_state["saves"]["42"]["active_slot"] == "desktop"
        # The server save was downloaded
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) >= 1

    @pytest.mark.asyncio
    async def test_pending_uploads_blocked(self, tmp_path):
        """Local file changed since last sync → switch blocked with reason + file list."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"modified save data")

        # State records an *old* hash — hash mismatch simulates pending upload
        old_hash = hashlib.md5(b"original save data").hexdigest()
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": old_hash,
                    "last_sync_at": "2026-01-01T00:00:00Z",
                    "tracked_save_id": 100,
                },
            },
            "active_slot": "default",
            "slot_confirmed": True,
        }

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is False
        assert result["reason"] == "pending_uploads"
        assert "pokemon.srm" in result["files"]
        # No downloads should have happened
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0

    @pytest.mark.asyncio
    async def test_never_synced_not_blocked(self, tmp_path):
        """Local save exists but was never synced (no last_sync_hash) → switch NOT blocked.

        Never-synced files will be deleted during the switch, so they must not block it.
        After the switch to an empty slot the local file should be gone.
        """
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # State has the game entry but no last_sync_hash for the file
        svc._save_sync_state["saves"]["42"] = {
            "files": {},  # no entry for pokemon.srm at all
            "active_slot": "default",
            "slot_confirmed": True,
        }

        # No server saves in "desktop" slot → switch succeeds and deletes local file
        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is True
        assert not save_path.exists()

    @pytest.mark.asyncio
    async def test_server_unreachable(self, tmp_path):
        """list_saves raises → switch blocked with reason=server_unreachable."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Files synced so readiness check passes
        svc._save_sync_state["saves"]["42"] = self._synced_state(local_hash)

        fake.fail_on_next(RommApiError(503, "Service unavailable"))

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is False
        assert result["reason"] == "server_unreachable"

    @pytest.mark.asyncio
    async def test_sync_disabled(self, tmp_path):
        """Save sync disabled → immediate error, no API calls."""
        svc, fake = make_service(tmp_path)
        # save_sync_enabled defaults to False
        _install_rom(svc, tmp_path)

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is False
        assert result["reason"] == "sync_disabled"
        assert len(fake.call_log) == 0

    @pytest.mark.asyncio
    async def test_not_installed(self, tmp_path):
        """ROM not installed → returns not_installed error."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        # ROM 42 is NOT installed

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is False
        assert result["reason"] == "not_installed"

    @pytest.mark.asyncio
    async def test_empty_new_slot(self, tmp_path):
        """New slot has no saves on server → deletes local files and updates active_slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Files synced so readiness check passes
        svc._save_sync_state["saves"]["42"] = self._synced_state(local_hash)

        # Server has no saves in "newslot" (all fake saves are in other slots)
        fake.saves[300] = _server_save(save_id=300, slot="other")

        result = await svc.switch_slot(42, "newslot")

        assert result["success"] is True
        assert svc._save_sync_state["saves"]["42"]["active_slot"] == "newslot"
        # No downloads
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0
        # Local file deleted (fresh start for empty slot)
        assert not save_path.exists()
        # File tracking state cleared
        assert svc._save_sync_state["saves"]["42"]["files"] == {}

    @pytest.mark.asyncio
    async def test_empty_slot_deletes_local_files(self, tmp_path):
        """New slot is empty → local save files deleted and file tracking cleared."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Current slot is fully synced
        svc._save_sync_state["saves"]["42"] = self._synced_state(local_hash)

        # No server saves for "brand-new-slot"
        result = await svc.switch_slot(42, "brand-new-slot")

        assert result["success"] is True
        assert svc._save_sync_state["saves"]["42"]["active_slot"] == "brand-new-slot"
        # Local save file removed
        assert not save_path.exists()
        # File tracking state cleared so next play starts fresh
        assert svc._save_sync_state["saves"]["42"]["files"] == {}
        # No downloads happened
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0

    @pytest.mark.asyncio
    async def test_with_server_saves_downloads(self, tmp_path):
        """New slot has server saves → downloads them, replacing local file."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"old local save")
        local_hash = _file_md5(str(save_path))

        # Current slot is fully synced
        svc._save_sync_state["saves"]["42"] = self._synced_state(local_hash)

        # Target slot has a server save
        fake.saves[500] = _server_save(save_id=500, slot="target-slot")

        result = await svc.switch_slot(42, "target-slot")

        assert result["success"] is True
        assert svc._save_sync_state["saves"]["42"]["active_slot"] == "target-slot"
        # Server save was downloaded (replaces local)
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) >= 1

    @pytest.mark.asyncio
    async def test_no_local_files_is_ready(self, tmp_path):
        """ROM installed but no local save files → readiness check passes (nothing pending)."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        # No save file created on disk
        svc._save_sync_state["saves"]["42"] = {
            "files": {},
            "active_slot": "default",
            "slot_confirmed": True,
        }

        fake.saves[100] = _server_save(save_id=100, slot="desktop")

        result = await svc.switch_slot(42, "desktop")

        assert result["success"] is True
        assert svc._save_sync_state["saves"]["42"]["active_slot"] == "desktop"

    @pytest.mark.asyncio
    async def test_switch_to_legacy_slot(self, tmp_path):
        """switch_slot("") sets active_slot=None, persists "" in slots dict, returns success."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        # Start in a named slot, fully synced
        svc._save_sync_state["saves"]["42"] = self._synced_state(local_hash)
        svc._save_sync_state["saves"]["42"]["active_slot"] = "default"

        # Server has a legacy save (slot=None)
        fake.saves[200] = _server_save(save_id=200, slot=None)

        result = await svc.switch_slot(42, "")

        assert result["success"] is True
        assert "save_status" in result
        # active_slot in state is None (legacy)
        assert svc._save_sync_state["saves"]["42"]["active_slot"] is None
        # Legacy slot "" appears in the slots dict
        slots_dict = svc._save_sync_state["saves"]["42"].get("slots", {})
        assert "" in slots_dict

    @pytest.mark.asyncio
    async def test_legacy_slot_persisted_in_get_save_slots(self, tmp_path):
        """get_save_slots includes the "" entry when active_slot is None and "" is in slots dict."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        # Set up state with legacy slot explicitly
        svc._save_sync_state["saves"]["99"] = {
            "active_slot": None,
            "slot_confirmed": True,
            "files": {},
            "slots": {"": {"source": "local", "count": 0, "latest_updated_at": None}},
        }

        # Server returns no slots
        result = await svc.get_save_slots(99)

        assert result["success"] is True
        # The "" entry should be in the response slots list
        slot_names = [s["slot"] for s in result["slots"]]
        assert "" in slot_names
        # active_slot is None (legacy)
        assert result["active_slot"] is None

    @pytest.mark.asyncio
    async def test_server_legacy_save_maps_to_empty_string_not_default(self, tmp_path):
        """Server saves with slot=None (legacy) must map to "" not "default" in get_save_slots."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "dev-1"

        # Server has a legacy save with slot=None
        fake.saves[1] = {
            "id": 1,
            "rom_id": 77,
            "file_name": "game.srm",
            "updated_at": "2026-04-07T10:00:00",
            "slot": None,
        }

        result = await svc.get_save_slots(77)

        assert result["success"] is True
        slot_names = [s["slot"] for s in result["slots"]]
        # Must be "" (legacy key), NOT "default"
        assert "" in slot_names
        assert "default" not in slot_names


# ---------------------------------------------------------------------------
# TestListFileVersions
# ---------------------------------------------------------------------------


class TestListFileVersions:
    """Tests for SaveService.list_file_versions."""

    def _setup_state(self, svc, tracked_id: int | None) -> None:
        """Populate save state with a tracked save id for rom 42, pokemon.srm."""
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "files": {
                "pokemon.srm": {"tracked_save_id": tracked_id},
            },
        }

    @pytest.mark.asyncio
    async def test_happy_path_excludes_tracked(self, tmp_path):
        """Returns every save in the slot except the currently-tracked one.

        The slot is the unit, not the filename — saves with different
        ``file_name_no_tags`` (uploaded by RomM web UI or third-party
        clients with their own naming) are first-class versions and
        surface alongside saves whose names match the local filename.
        """
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-01T10:00:00Z")
        # Foreign-client upload with a different basename — still a valid
        # version of the same slot, must appear in the version list.
        fake.saves[60] = {
            "id": 60,
            "rom_id": 42,
            "file_name": "other.srm",
            "file_name_no_tags": "other",
            "file_extension": "srm",
            "updated_at": "2026-03-05T10:00:00Z",
            "file_size_bytes": 512,
            "slot": "default",
            "download_path": "/saves/other.srm",
        }
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        ids_in_order = [v["id"] for v in result]
        assert ids_in_order == [60, 50]  # newest first; tracked id=100 excluded

    @pytest.mark.asyncio
    async def test_foreign_tracked_save_does_not_hide_local_versions(self, tmp_path):
        """When the tracked save was uploaded by another client with a
        different basename, legitimate versions of the local file still
        surface — they are no longer filtered against the tracked save's
        ``file_name_no_tags``.
        """
        svc, fake = make_service(tmp_path)

        # Tracked save with a foreign naming scheme.
        fake.saves[100] = _server_save(
            save_id=100,
            rom_id=42,
            slot="default",
            updated_at="2026-03-10T10:00:00Z",
            filename="alt_name [2026-03-10_10-00-00].srm",
            file_name_no_tags="alt_name",
        )
        fake.saves[50] = _server_save(
            save_id=50,
            rom_id=42,
            slot="default",
            updated_at="2026-03-01T10:00:00Z",
            filename="pokemon [2026-03-01_10-00-00].srm",
            file_name_no_tags="pokemon",
        )
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert len(result) == 1
        assert result[0]["id"] == 50

    @pytest.mark.asyncio
    async def test_sorted_newest_first(self, tmp_path):
        """Versions are sorted by updated_at descending."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[30] = _server_save(save_id=30, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-01T10:00:00Z")
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert len(result) == 2
        assert result[0]["id"] == 50  # newer of the two old versions first
        assert result[1]["id"] == 30

    @pytest.mark.asyncio
    async def test_empty_when_no_older_versions(self, tmp_path):
        """Returns empty list when there are no versions other than the tracked one."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result == []

    @pytest.mark.asyncio
    async def test_no_tracked_save_returns_every_save_in_slot(self, tmp_path):
        """Without ``tracked_save_id`` in state, every save in the slot is
        returned — there is no tracked save to exclude."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-01T10:00:00Z")
        # No state at all (tracked_id is None)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert {v["id"] for v in result} == {100, 50}

    @pytest.mark.asyncio
    async def test_api_error_returns_empty(self, tmp_path):
        """Returns empty list when the server call fails."""
        svc, fake = make_service(tmp_path)

        fake.fail_on_next(Exception("network error"))

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result == []

    @pytest.mark.asyncio
    async def test_result_shape(self, tmp_path):
        """Each entry contains the required fields: id, updated_at, file_size_bytes, device_syncs."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = {
            "id": 50,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-01_10-00-00].srm",
            "file_name_no_tags": "pokemon",
            "emulator": "retroarch-mgba",
            "updated_at": "2026-03-01T10:00:00Z",
            "file_size_bytes": 2048,
            "device_syncs": [
                {"device_id": "abc", "device_name": "steamdeck", "is_current": True, "last_synced_at": None}
            ],
            "slot": "default",
            "download_path": "/saves/pokemon.srm",
        }
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert len(result) == 1
        entry = result[0]
        assert entry["id"] == 50
        assert entry["file_name"] == "pokemon [2026-03-01_10-00-00].srm"
        assert entry["emulator"] == "retroarch-mgba"
        assert entry["updated_at"] == "2026-03-01T10:00:00Z"
        assert entry["file_size_bytes"] == 2048
        assert len(entry["device_syncs"]) == 1
        assert entry["device_syncs"][0]["device_name"] == "steamdeck"

    @pytest.mark.asyncio
    async def test_list_file_versions_populates_uploaded_by_us(self, tmp_path):
        """uploaded_by_us is True for IDs in own_upload_ids, False for others."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-05T10:00:00Z")
        fake.saves[30] = _server_save(save_id=30, rom_id=42, slot="default", updated_at="2026-03-01T10:00:00Z")

        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "own_upload_ids": [50],
            "files": {
                "pokemon.srm": {"tracked_save_id": 100},
            },
        }

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert len(result) == 2
        by_id = {v["id"]: v for v in result}
        assert by_id[50]["uploaded_by_us"] is True
        assert by_id[30]["uploaded_by_us"] is False

    @pytest.mark.asyncio
    async def test_list_file_versions_legacy_state_returns_none(self, tmp_path):
        """When rom state has no own_upload_ids key, uploaded_by_us is None for all versions."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-05T10:00:00Z")

        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            # own_upload_ids key intentionally absent — legacy state
            "files": {
                "pokemon.srm": {"tracked_save_id": 100},
            },
        }

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert len(result) == 1
        assert result[0]["uploaded_by_us"] is None


# ---------------------------------------------------------------------------
# TestRollbackToVersion
# ---------------------------------------------------------------------------


class TestRollbackToVersion:
    """Tests for SaveService.rollback_to_version — the core rollback flow."""

    def _setup_state(self, svc, tmp_path, tracked_id: int, last_sync_hash: str | None = None) -> None:
        _install_rom(svc, tmp_path)
        _enable_sync_with_device(svc)
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": tracked_id,
                    "last_sync_hash": last_sync_hash,
                },
            },
        }

    @staticmethod
    def _tracked_save(save_id: int, *, updated_at: str = "2026-03-10T10:00:00Z") -> dict:
        """Build a tracked-save fixture with our device flagged ``is_current``
        on it. Use this for tests where the matrix pre-flight should return
        ``Skip(synced)`` so the switch flow itself is what's exercised.
        """
        return _server_save_with_syncs(
            save_id=save_id,
            slot="default",
            updated_at=updated_at,
            device_syncs=[{"device_id": "device-1", "is_current": True, "last_synced_at": updated_at}],
        )

    @pytest.mark.asyncio
    async def test_returns_not_found_when_rom_not_installed(self, tmp_path):
        """Returns not_found when the ROM is not in installed_roms."""
        svc, _fake = make_service(tmp_path)

        # rom 999 is not installed
        result = await svc.rollback_to_version(999, "default", 50)
        assert result == {"status": "not_found"}

    @pytest.mark.asyncio
    async def test_returns_not_found_when_save_id_missing(self, tmp_path):
        """Returns not_found when target save_id is not in the server response."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        # Request save_id=999, which doesn't exist
        result = await svc.rollback_to_version(42, "default", 999)
        assert result == {"status": "not_found"}

    @pytest.mark.asyncio
    async def test_proceeds_even_with_newer_foreign_save(self, tmp_path):
        """Switch is not blocked when a newer foreign save exists in the slot.

        Gate E was removed — switching to a previous version is an explicit
        user action. The newer foreign save still gets adopted by the
        pre-flight (matrix returns Download), then the switch proceeds.
        """
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        # newer save from another device — pre-flight adopts it, switch then
        # bumps id=50 above it.
        fake.saves[200] = _server_save(save_id=200, rom_id=42, slot="default", updated_at="2026-03-20T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_tracked_missing_does_not_block(self, tmp_path):
        """Switch proceeds when the currently-tracked save is gone from the
        server. The matrix pre-flight runs against whatever's actually in
        the slot and the user's chosen target survives that run.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        self._setup_state(svc, tmp_path, tracked_id=999)
        _create_save(tmp_path)
        # Tracked save 999 does NOT exist on server. Only save 50 is present
        # (and is the rollback target).
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unsynced_changes_get_uploaded_before_switch(self, tmp_path):
        """Local diverged from last_sync_hash + we're flagged ``is_current`` on
        the tracked save → matrix returns ``Upload(PUT)`` → pre-flight
        silently pushes local up to the server → switch proceeds. No warning,
        no force flag, no data loss.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash="aabbcc001122334455667788")
        _create_save(tmp_path, content=b"\xff" * 1024)

        fake.saves[100] = _server_save_with_syncs(
            save_id=100,
            slot="default",
            updated_at="2026-03-10T10:00:00Z",
            device_syncs=[{"device_id": "device-1", "is_current": True, "last_synced_at": "2026-03-10T10:00:00Z"}],
        )
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # Pre-flight PUT against id=100 happened (the silent upload of local
        # changes), then switch PUT against id=50 (the rollback bump).
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        upload_targets = [c[2].get("save_id") for c in upload_calls]
        assert 100 in upload_targets  # pre-flight PUT
        assert 50 in upload_targets  # switch PUT

    @pytest.mark.asyncio
    async def test_unsynced_with_server_moved_blocks_via_conflict(self, tmp_path):
        """Local diverged + we're not ``is_current`` (someone else uploaded) →
        matrix returns ``Conflict`` → switch returns ``conflict_blocked`` and
        does no I/O on the rollback target. The frontend resolves via the
        standard SyncConflictModal.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash="aabbcc001122334455667788")
        _create_save(tmp_path, content=b"\xff" * 1024)

        fake.saves[100] = _server_save_with_syncs(
            save_id=100,
            slot="default",
            updated_at="2026-03-15T10:00:00Z",
            device_syncs=[{"device_id": "device-1", "is_current": False, "last_synced_at": "2026-03-10T10:00:00Z"}],
        )
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "conflict_blocked"
        assert len(result["conflicts"]) == 1
        # No download of the rollback target should have happened.
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert not any(c[1][0] == 50 for c in download_calls)

    @pytest.mark.asyncio
    async def test_happy_path_downloads_and_updates_state(self, tmp_path):
        """Happy path: rollback downloads the target save and updates file sync state."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        # tracked (no change since last sync)
        fake.saves[100] = self._tracked_save(100)
        # older version to roll back to
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # Verify download was called with save_id=50
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert any(c[1][0] == 50 for c in download_calls)
        # State updated: tracked_save_id should now point to the rolled-back save
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 50

    @pytest.mark.asyncio
    async def test_no_local_file_pre_flight_downloads_then_switch_proceeds(self, tmp_path):
        """When local file doesn't exist, the matrix pre-flight pulls the
        server's tracked save (recovery path) before the switch overwrites it
        with the chosen older version."""
        svc, fake = make_service(tmp_path)

        # No local file. Tracked save 100 exists with our device current.
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash="somehash")
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # Both saves were downloaded: 100 from pre-flight recovery, 50 from
        # the actual switch.
        download_ids = [c[1][0] for c in fake.call_log if c[0] == "download_save_content"]
        assert 100 in download_ids
        assert 50 in download_ids

    @pytest.mark.asyncio
    async def test_no_state_entry_proceeds_via_baseline_adoption(self, tmp_path):
        """No ``last_sync_hash`` baseline yet → matrix returns
        ``Skip(adopt_baseline=True)`` for the tracked save → pre-flight is a
        no-op state hygiene write → switch proceeds normally."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path, content=b"\xff" * 1024)
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=None)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_server_error_returns_preflight_failed(self, tmp_path):
        """Server failure during the matrix pre-flight aborts the switch
        with ``preflight_failed`` (the rollback never runs)."""
        svc, fake = make_service(tmp_path)

        _install_rom(svc, tmp_path)
        _enable_sync_with_device(svc)
        fake.fail_on_next(Exception("network error"))

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "preflight_failed"
        assert any("network" in err.lower() for err in result.get("errors", []))

    # ------------------------------------------------------------------
    # Cross-device propagation: rollback re-PUTs target so it becomes
    # newest-in-slot and the rollback target wins on other devices' next sync.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_rollback_uploads_target_after_download(self, tmp_path):
        """Rollback PUTs the target save_id after downloading so updated_at bumps server-side."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # Verify a PUT (upload_save with save_id=50) fired after download
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert any(c[2].get("save_id") == 50 for c in upload_calls), f"expected PUT to save_id=50, got {upload_calls!r}"

    @pytest.mark.asyncio
    async def test_rollback_calls_confirm_download_on_target(self, tmp_path):
        """Rollback marks our device as current on the target save via confirm_download."""
        svc, fake = make_service(tmp_path)
        # device_id must be set for confirm_download to fire
        svc._save_sync_state["server_device_id"] = "device-1"

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert any(c[1] == (50, "device-1") for c in confirm_calls), (
            f"expected confirm_download(50, 'device-1'), got {confirm_calls!r}"
        )

    @pytest.mark.asyncio
    async def test_rollback_updates_tracked_id_and_hash(self, tmp_path):
        """After rollback, local state has tracked_save_id=target and last_sync_hash matches downloaded content."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 50
        # Hash should match the (re-uploaded) local file content
        local_path = tmp_path / "saves" / "gba" / "pokemon.srm"
        assert file_state["last_sync_hash"] == _file_md5(str(local_path))
        # last_sync_server_updated_at reflects the post-PUT response (NOT the
        # pre-PUT target.updated_at), confirming the bump propagated locally.
        assert file_state["last_sync_server_updated_at"] != "2026-02-01T10:00:00Z"

    @pytest.mark.asyncio
    async def test_rollback_returns_put_failed_when_upload_raises(self, tmp_path):
        """When the PUT step fails, status reflects put_failed and download survives.

        Partial-rollback semantics: the local download already mutated state to
        point at the target save_id, so the rollback IS locally complete. We
        persist that state (saved on put_failed) so a restart doesn't leave a
        state/file mismatch. Cross-device propagation is what failed; the user
        can retry rollback_to_version idempotently.
        """
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        # Track call sequence: download must succeed, then upload must fail.
        original_upload = fake.upload_save

        def failing_upload(*args, **kwargs):
            raise Exception("PUT failed: server 500")

        fake.upload_save = failing_upload  # type: ignore[method-assign]

        try:
            result = await svc.rollback_to_version(42, "default", 50)
        finally:
            fake.upload_save = original_upload  # type: ignore[method-assign]

        assert result["status"] == "put_failed"
        assert "PUT failed" in result.get("error", "")
        # Download did happen
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert any(c[1][0] == 50 for c in download_calls)
        # Local state still updated to point at target — save_state was called
        # so disk file and state file remain consistent.
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 50

    @pytest.mark.asyncio
    async def test_rollback_ignores_confirm_download_failure(self, tmp_path):
        """confirm_download failure is non-fatal — rollback still reports ok and updates state.

        ``_do_upload_save`` swallows confirm_download errors (debug-logged).
        From the rollback's perspective the PUT succeeded, so cross-device
        propagation works (the next list_saves still picks our bumped
        ``updated_at``). is_current may not be set on our device until the next
        sync, but that's recoverable — the next compute_sync_action will
        detect tracked_save_id==server.id and Skip.
        """
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["server_device_id"] = "device-1"

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        original_confirm = fake.confirm_download

        def failing_confirm(*args, **kwargs):
            raise Exception("confirm_download network error")

        fake.confirm_download = failing_confirm  # type: ignore[method-assign]

        try:
            result = await svc.rollback_to_version(42, "default", 50)
        finally:
            fake.confirm_download = original_confirm  # type: ignore[method-assign]

        assert result["status"] == "ok"
        # Upload still happened
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert any(c[2].get("save_id") == 50 for c in upload_calls)
        # State updated
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 50

    @pytest.mark.asyncio
    async def test_rollback_to_already_tracked_save_is_idempotent(self, tmp_path):
        """Rolling back to the currently-tracked save still PUTs (idempotent bump)."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        # Tracked save IS the target — rollback to currently-owned save
        self._setup_state(svc, tmp_path, tracked_id=50, last_sync_hash=local_hash)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # PUT still fired (bumps updated_at, idempotent re-confirm)
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert any(c[2].get("save_id") == 50 for c in upload_calls)
        # tracked_save_id still 50
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 50


# ---------------------------------------------------------------------------
# TestDeleteSlot
# ---------------------------------------------------------------------------


class TestDeleteSlot:
    """Tests for SaveService.delete_slot and get_slot_delete_info."""

    def _setup_state_with_slots(
        self,
        svc,
        tmp_path,
        *,
        active_slot="default",
        extra_slots=None,
        files_state=None,
    ):
        """Set up a ROM with slot state for deletion tests."""
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "server-dev-1"
        _install_rom(svc, tmp_path)

        slots = {
            "default": {"source": "server", "count": 1, "latest_updated_at": "2026-03-24T10:00:00"},
        }
        if extra_slots:
            slots.update(extra_slots)

        svc._save_sync_state["saves"]["42"] = {
            "active_slot": active_slot,
            "slot_confirmed": True,
            "slots": slots,
            "files": files_state or {},
        }

    @pytest.mark.asyncio
    async def test_get_slot_delete_info_server_slot(self, tmp_path):
        """Server slot returns save count and tracked file info."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"save1": {"source": "server", "count": 3, "latest_updated_at": None}},
            files_state={
                "pokemon.srm": {"tracked_save_id": 10, "last_sync_hash": "abc"},
                "zelda.srm": {"tracked_save_id": 11, "last_sync_hash": "def"},
                "unrelated.srm": {"tracked_save_id": 99, "last_sync_hash": "ghi"},
            },
        )
        fake.saves[10] = _server_save(save_id=10, rom_id=42, filename="pokemon.srm", slot="save1")
        fake.saves[11] = _server_save(save_id=11, rom_id=42, filename="zelda.srm", slot="save1")
        fake.saves[12] = _server_save(save_id=12, rom_id=42, filename="extra.srm", slot="save1")

        result = await svc.get_slot_delete_info(42, "save1")

        assert result["success"] is True
        assert result["server_save_count"] == 3
        assert set(result["server_save_ids"]) == {10, 11, 12}
        assert result["local_file_count"] == 2
        assert set(result["local_filenames"]) == {"pokemon.srm", "zelda.srm"}
        assert result["is_active"] is False

    @pytest.mark.asyncio
    async def test_get_slot_delete_info_local_only_slot(self, tmp_path):
        """Local-only slot returns zero server saves."""
        svc, _fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"local1": {"source": "local", "count": 0, "latest_updated_at": None}},
        )

        result = await svc.get_slot_delete_info(42, "local1")

        assert result["success"] is True
        assert result["source"] == "local"
        assert result["server_save_count"] == 0
        assert result["local_file_count"] == 0

    @pytest.mark.asyncio
    async def test_get_slot_delete_info_active_slot(self, tmp_path):
        """Info for the active slot still returns data (is_active=True)."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(svc, tmp_path, active_slot="default")
        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default")

        result = await svc.get_slot_delete_info(42, "default")

        assert result["success"] is True
        assert result["is_active"] is True
        assert result["server_save_count"] == 1

    @pytest.mark.asyncio
    async def test_get_slot_delete_info_nonexistent_slot(self, tmp_path):
        """Non-existent slot returns not_found."""
        svc, _fake = make_service(tmp_path)
        self._setup_state_with_slots(svc, tmp_path)

        result = await svc.get_slot_delete_info(42, "nonexistent")

        assert result["success"] is False
        assert result["reason"] == "not_found"

    @pytest.mark.asyncio
    async def test_delete_slot_server_saves_success(self, tmp_path):
        """Deleting a server slot removes server saves and cleans up state."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"save1": {"source": "server", "count": 2, "latest_updated_at": None}},
            files_state={
                "pokemon.srm": {"tracked_save_id": 10, "last_sync_hash": "abc"},
                "zelda.srm": {"tracked_save_id": 11, "last_sync_hash": "def"},
            },
        )
        fake.saves[10] = _server_save(save_id=10, rom_id=42, filename="pokemon.srm", slot="save1")
        fake.saves[11] = _server_save(save_id=11, rom_id=42, filename="zelda.srm", slot="save1")

        result = await svc.delete_slot(42, "save1")

        assert result["success"] is True
        assert result["deleted_server_saves"] == 2
        assert result["cleaned_files"] == 2
        # Slot removed from state
        assert "save1" not in svc._save_sync_state["saves"]["42"]["slots"]
        # File entries cleaned
        assert "pokemon.srm" not in svc._save_sync_state["saves"]["42"]["files"]
        assert "zelda.srm" not in svc._save_sync_state["saves"]["42"]["files"]
        # delete_server_saves called with correct IDs
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 1
        assert set(delete_calls[0][1][0]) == {10, 11}

    @pytest.mark.asyncio
    async def test_delete_slot_local_only_success(self, tmp_path):
        """Deleting a local-only slot skips server calls."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"local1": {"source": "local", "count": 0, "latest_updated_at": None}},
        )

        result = await svc.delete_slot(42, "local1")

        assert result["success"] is True
        assert result["deleted_server_saves"] == 0
        assert "local1" not in svc._save_sync_state["saves"]["42"]["slots"]
        # No server calls made
        delete_calls = [c for c in fake.call_log if c[0] == "delete_server_saves"]
        assert len(delete_calls) == 0

    @pytest.mark.asyncio
    async def test_delete_slot_blocks_active_slot(self, tmp_path):
        """Cannot delete the active slot."""
        svc, _fake = make_service(tmp_path)
        self._setup_state_with_slots(svc, tmp_path, active_slot="default")

        result = await svc.delete_slot(42, "default")

        assert result["success"] is False
        assert result["reason"] == "active_slot"
        # Slot still exists
        assert "default" in svc._save_sync_state["saves"]["42"]["slots"]

    @pytest.mark.asyncio
    async def test_delete_slot_server_error(self, tmp_path):
        """Server error leaves slot intact (no partial cleanup)."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"save1": {"source": "server", "count": 1, "latest_updated_at": None}},
        )
        fake.saves[10] = _server_save(save_id=10, rom_id=42, filename="pokemon.srm", slot="save1")
        # First list_saves call succeeds, then delete_server_saves fails
        original_delete = fake.delete_server_saves

        def fail_delete(save_ids):
            raise RommApiError(500, "Server error")

        fake.delete_server_saves = fail_delete

        result = await svc.delete_slot(42, "save1")

        assert result["success"] is False
        assert result["reason"] == "server_error"
        # Slot NOT removed from state (rollback on failure)
        assert "save1" in svc._save_sync_state["saves"]["42"]["slots"]

        fake.delete_server_saves = original_delete

    @pytest.mark.asyncio
    async def test_delete_slot_cleans_up_tracked_files(self, tmp_path):
        """Only file entries pointing to deleted saves are removed; unrelated entries preserved."""
        svc, fake = make_service(tmp_path)
        self._setup_state_with_slots(
            svc,
            tmp_path,
            extra_slots={"save1": {"source": "server", "count": 2, "latest_updated_at": None}},
            files_state={
                "pokemon.srm": {"tracked_save_id": 10, "last_sync_hash": "abc"},
                "zelda.srm": {"tracked_save_id": 11, "last_sync_hash": "def"},
                "unrelated.srm": {"tracked_save_id": 99, "last_sync_hash": "ghi"},
            },
        )
        fake.saves[10] = _server_save(save_id=10, rom_id=42, filename="pokemon.srm", slot="save1")
        fake.saves[11] = _server_save(save_id=11, rom_id=42, filename="zelda.srm", slot="save1")

        result = await svc.delete_slot(42, "save1")

        assert result["success"] is True
        files = svc._save_sync_state["saves"]["42"]["files"]
        assert "pokemon.srm" not in files
        assert "zelda.srm" not in files
        assert "unrelated.srm" in files
        assert files["unrelated.srm"]["tracked_save_id"] == 99

    @pytest.mark.asyncio
    async def test_delete_slot_not_installed_rom(self, tmp_path):
        """ROM not installed returns failure."""
        svc, _fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        # Don't install any ROM

        result = await svc.delete_slot(42, "default")

        assert result["success"] is False
        assert result["reason"] == "not_installed"

    @pytest.mark.asyncio
    async def test_delete_slot_sync_disabled(self, tmp_path):
        """Save sync disabled returns failure."""
        svc, _fake = make_service(tmp_path)
        # save_sync_enabled defaults to False

        result = await svc.delete_slot(42, "default")

        assert result["success"] is False
        assert result["reason"] == "disabled"


# ---------------------------------------------------------------------------
# TestOwnUploadIds
# ---------------------------------------------------------------------------


class TestOwnUploadIds:
    """Tests for own_upload_ids tracking and the uploaded_by_us flag."""

    @pytest.mark.asyncio
    async def test_post_upload_appends_own_upload_id(self, tmp_path):
        """After a POST upload (new save), the returned save_id is added to own_upload_ids."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        # No pre-existing server save — this will be a POST (save_id=None)
        await svc.sync_rom_saves(42)

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        returned_id = upload_calls[0][2]["save_id"]  # save_id kwarg from upload_save call
        # The save_id passed to upload_save should be None (POST path)
        assert returned_id is None

        rom_state = svc._save_sync_state["saves"]["42"]
        own_ids = rom_state.get("own_upload_ids", [])
        assert len(own_ids) == 1
        # The id in the list must match what fake returned
        new_save_id = next(iter(fake.saves.values()))["id"]
        assert new_save_id in own_ids

    @pytest.mark.asyncio
    async def test_post_upload_idempotent_in_own_list(self, tmp_path):
        """Calling _do_upload_save twice with the same resulting save_id does not duplicate."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_file = _create_save(tmp_path)

        # Pre-populate own_upload_ids with the id that fake will return (1000)
        svc._save_sync_state["saves"]["42"] = {
            "files": {},
            "system": "gba",
            "active_slot": "default",
            "own_upload_ids": [1000],
        }
        # Fake will return the same id=1000 because filename matches existing
        fake.saves[1000] = _server_save(save_id=1000, rom_id=42)

        # Call internal upload with no server_save (POST path)
        svc._do_upload_save(42, str(save_file), "pokemon.srm", "42", "gba", server_save=None)

        rom_state = svc._save_sync_state["saves"]["42"]
        # Should still have exactly one entry for that id
        assert rom_state["own_upload_ids"].count(1000) == 1

    @pytest.mark.asyncio
    async def test_put_upload_does_not_touch_own_list(self, tmp_path):
        """Updating an existing tracked save (PUT path) does not modify own_upload_ids."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_file = _create_save(tmp_path)

        # Pre-existing server save (id=100) — upload_save called with save_id=100 → PUT
        fake.saves[100] = _server_save(save_id=100, rom_id=42)
        svc._save_sync_state["saves"]["42"] = {
            "files": {"pokemon.srm": {"tracked_save_id": 100, "last_sync_hash": "old"}},
            "system": "gba",
            "active_slot": "default",
            "own_upload_ids": [99],  # pre-existing unrelated id
        }

        server_save = fake.saves[100]
        svc._do_upload_save(42, str(save_file), "pokemon.srm", "42", "gba", server_save=server_save)

        rom_state = svc._save_sync_state["saves"]["42"]
        # own_upload_ids must not have changed (100 not added, 99 still there)
        assert rom_state["own_upload_ids"] == [99]

    @pytest.mark.asyncio
    async def test_get_save_status_legacy_rom_state_returns_none(self, tmp_path):
        """When rom state exists but own_upload_ids key is absent, uploaded_by_us is None."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        fake.saves[26] = _server_save(save_id=26, rom_id=42, filename="pokemon.srm")

        # Legacy state: own_upload_ids key is absent
        svc._save_sync_state["saves"]["42"] = {
            "files": {},
            "system": "gba",
            "active_slot": None,
            # no own_upload_ids key
        }

        result = await svc.get_save_status(42)

        files_by_id = {f["server_save_id"]: f for f in result["files"] if f.get("server_save_id")}
        assert files_by_id[26]["uploaded_by_us"] is None

    @pytest.mark.asyncio
    async def test_rollback_to_foreign_version_preserves_own_upload_ids(self, tmp_path):
        """Rolling back to a foreign save (not in own_upload_ids) does not modify own_upload_ids."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        save_file = _create_save(tmp_path)
        local_hash = _file_md5(str(save_file))

        # own save is 26, tracked is 26 (clean state)
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 26,
                    "last_sync_hash": local_hash,
                }
            },
            "system": "gba",
            "active_slot": "default",
            "own_upload_ids": [26],
        }
        fake.saves[26] = _server_save(save_id=26, rom_id=42, slot="default")
        # Foreign older version to roll back to
        fake.saves[27] = _server_save(save_id=27, rom_id=42, slot="default", updated_at="2026-01-01T00:00:00Z")

        result = await svc.rollback_to_version(42, "default", 27)

        assert result["status"] == "ok"
        # own_upload_ids must be unchanged — 27 was not POSTed by us
        rom_state = svc._save_sync_state["saves"]["42"]
        assert rom_state["own_upload_ids"] == [26]


# ---------------------------------------------------------------------------
# compute_sync_action dispatch — service-level coverage
# ---------------------------------------------------------------------------
#
# Tests below pin the service's behaviour when ``compute_sync_action`` returns
# each ``SyncAction`` outcome. They cover the ``_sync_rom_saves`` dispatch,
# the read-only ``_get_save_status_io`` parity path, the two-action
# ``resolve_sync_conflict`` callable, and per-rom-lock serialization across
# concurrent ``sync_rom_saves`` calls.


def _enable_sync_with_device(svc, device_id: str = "device-1") -> None:
    """Flip on save sync and bind a server device id (matches FakeSaveApi)."""
    svc._save_sync_state["settings"]["save_sync_enabled"] = True
    svc._save_sync_state["device_id"] = device_id
    svc._save_sync_state["server_device_id"] = device_id


def _server_save_with_syncs(
    *,
    save_id: int = 100,
    rom_id: int = 42,
    filename: str = "pokemon.srm",
    updated_at: str = "2026-02-17T06:00:00Z",
    file_size_bytes: int = 1024,
    device_syncs: list[dict] | None = None,
    slot: str | None = None,
) -> dict:
    """Build a server-save dict with explicit device_syncs (no FakeApi shimming)."""
    base = _server_save(
        save_id=save_id,
        rom_id=rom_id,
        filename=filename,
        updated_at=updated_at,
        file_size_bytes=file_size_bytes,
    )
    if slot is not None:
        base["slot"] = slot
    base["device_syncs"] = device_syncs if device_syncs is not None else []
    return base


# ---------------------------------------------------------------------------
# _sync_rom_saves dispatch
# ---------------------------------------------------------------------------


class TestSyncRomSavesDispatch:
    def test_sync_rom_saves_skip_when_synced(self, tmp_path):
        """is_current=true + matching hash + tracked → Skip, no I/O."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"pristine save")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": ss["updated_at"],
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 0
        assert errors == []
        assert conflicts == []
        # No upload/download initiated.
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)

    def test_sync_rom_saves_upload_post_when_no_server_save(self, tmp_path):
        """No server saves in slot but local exists → Upload (POST)."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"new local")

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # POST → save_id is None
        assert upload_calls[0][2]["save_id"] is None

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] is not None
        assert file_state["last_sync_hash"]

    def test_sync_rom_saves_download_when_server_changed(self, tmp_path):
        """is_current=false + local hash matches last_sync_hash → Download."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"unchanged local")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        # Download_save_content was called against the server save id.
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 1
        assert download_calls[0][1][0] == 100

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 100
        assert file_state["last_sync_hash"]  # updated to downloaded content's hash

    def test_sync_rom_saves_conflict_when_both_changed(self, tmp_path):
        """is_current=false + local hash diverges → Conflict, no I/O."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"diverged local")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "deadbeef" * 4,  # baseline differs from current local
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 0
        assert errors == []
        assert len(conflicts) == 1
        c = conflicts[0]
        assert isinstance(c, dict)
        assert c["type"] == "sync_conflict"
        assert c["rom_id"] == 42
        assert c["filename"] == "pokemon.srm"
        assert c["server_save_id"] == 100
        assert c["server_updated_at"] == ss["updated_at"]
        assert c["server_size"] == ss["file_size_bytes"]
        assert c["local_path"] == str(save_path)
        assert c["local_hash"] == local_hash
        assert c["local_mtime"] is not None
        assert c["local_size"] == os.path.getsize(str(save_path))
        assert "created_at" in c

    def test_sync_rom_saves_server_only_downloads(self, tmp_path):
        """No local file, one server save in slot → Download."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()

    def test_sync_rom_saves_upload_put_when_local_diverged(self, tmp_path):
        """is_current=true + local hash diverges from baseline → Upload (PUT)
        against the existing tracked save id."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"diverged offline")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "0" * 32,  # baseline differs from current local
                    "last_sync_server_updated_at": ss["updated_at"],
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT — save_id is the existing server save id
        assert upload_calls[0][2]["save_id"] == 100

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["last_sync_hash"] == local_hash

    def test_sync_rom_saves_skip_with_adopt_baseline_writes_hash(self, tmp_path):
        """is_current=true + local present + no baseline → Skip + adopt_baseline:
        no I/O but state.last_sync_hash gets recorded as local_hash."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"first sync")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        # No file_state at all — no baseline yet.
        svc._save_sync_state["saves"]["42"] = {"files": {}}

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 0
        assert errors == []
        assert conflicts == []
        # No I/O initiated.
        assert not any(c[0] in ("upload_save", "download_save_content", "download_save") for c in fake.call_log)
        # Baseline now persisted.
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["last_sync_hash"] == local_hash

    def test_sync_rom_saves_recovery_download_when_no_local(self, tmp_path):
        """is_current=true on the picked save but local file is gone → Download
        to recover the canonical content."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # No _create_save here — local file is absent.

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "abc",
                    "last_sync_server_updated_at": ss["updated_at"],
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 1
        assert download_calls[0][1][0] == 100
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()

    def test_dispatch_upload_put_targets_correct_save(self, tmp_path):
        """Dispatcher PUT: target_save_id selects the right server save from
        the candidate list and uploads against it."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-edit")

        ss = _server_save_with_syncs(
            save_id=100,
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        # Build a state where compute_sync_action emits Upload(target_save_id=100)
        # via the is_current=true + diverged hash branch.
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "0" * 32,
                    "last_sync_server_updated_at": ss["updated_at"],
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT — saved against the server save id provided by the algorithm.
        assert upload_calls[0][2]["save_id"] == 100
        # Local was not lost.
        assert save_path.read_bytes() == b"local-edit"

    def test_sync_rom_saves_persists_last_sync_check_at(self, tmp_path):
        """Every sync run records last_sync_check_at on the rom-level entry."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # Pure no-op: no local, no server saves.

        before = svc._save_sync_state["saves"].get("42", {}).get("last_sync_check_at")
        assert before is None

        svc._sync_rom_saves(42)

        after = svc._save_sync_state["saves"]["42"]["last_sync_check_at"]
        assert after is not None and isinstance(after, str)


# ---------------------------------------------------------------------------
# _get_save_status_io parity with compute_sync_action
# ---------------------------------------------------------------------------


class TestGetSaveStatusComputeAction:
    def test_get_save_status_returns_sync_conflict_shape(self, tmp_path):
        """When compute_sync_action emits Conflict, get_save_status surfaces it."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"diverged local")
        _ = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "0" * 32,
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }

        result = svc._get_save_status_io(42, [ss])

        assert len(result["conflicts"]) == 1
        c = result["conflicts"][0]
        assert isinstance(c, dict)
        assert c["type"] == "sync_conflict"
        assert c["rom_id"] == 42
        assert c["filename"] == "pokemon.srm"
        assert c["server_save_id"] == 100
        assert "created_at" in c

    def test_get_save_status_status_field_mapping(self, tmp_path):
        """Skip→synced, Upload→upload, Download→download, Conflict→conflict."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        # ---------- Skip ----------
        save_path = _create_save(tmp_path, content=b"matches baseline")
        local_hash = _file_md5(str(save_path))
        ss_skip = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": ss_skip["updated_at"],
                }
            }
        }
        result_skip = svc._get_save_status_io(42, [ss_skip])
        assert result_skip["files"][0]["status"] == "synced"

        # ---------- Upload ----------
        # Reset state for next case: no server saves
        svc._save_sync_state["saves"]["42"] = {"files": {}}
        result_upload = svc._get_save_status_io(42, [])
        assert result_upload["files"][0]["status"] == "upload"

        # ---------- Download ----------
        # Server moved past us, local matches baseline → Download
        ss_dl = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss_dl
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }
        result_dl = svc._get_save_status_io(42, [ss_dl])
        assert result_dl["files"][0]["status"] == "download"

        # ---------- Conflict ----------
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "0" * 32,
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }
        result_conflict = svc._get_save_status_io(42, [ss_dl])
        assert result_conflict["files"][0]["status"] == "conflict"

    def test_get_save_status_server_only_collapses_to_one_entry(self, tmp_path):
        """Multiple server saves in the active slot but no local file →
        exactly one entry returned (the newest server save), not one per
        server save. Older versions are reachable via list_file_versions."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # No local file.

        ss_old = _server_save_with_syncs(
            save_id=200,
            updated_at="2026-03-24T10:00:00",
            device_syncs=[{"device_id": "device-other", "is_current": True}],
        )
        ss_new = _server_save_with_syncs(
            save_id=201,
            updated_at="2026-03-24T15:00:00",
            device_syncs=[{"device_id": "device-other", "is_current": True}],
        )
        fake.saves[200] = ss_old
        fake.saves[201] = ss_new

        svc._save_sync_state["saves"]["42"] = {"files": {}}

        result = svc._get_save_status_io(42, [ss_old, ss_new])

        assert len(result["files"]) == 1
        entry = result["files"][0]
        assert entry["server_save_id"] == 201  # newest
        assert entry["status"] == "download"
        assert entry["local_path"] is None

    def test_get_save_status_empty_slot_returns_no_entries(self, tmp_path):
        """No local file and no server saves → files list is empty."""
        svc, _fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        svc._save_sync_state["saves"]["42"] = {"files": {}}

        result = svc._get_save_status_io(42, [])

        assert result["files"] == []
        assert result["conflicts"] == []


# ---------------------------------------------------------------------------
# resolve_sync_conflict (two-action user choice)
# ---------------------------------------------------------------------------


class TestResolveSyncConflict:
    @pytest.mark.asyncio
    async def test_resolve_keep_local_hash_match_short_circuits(self, tmp_path):
        """Local hash matches server's content hash → no PUT, state updated."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"identical content")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        # Make the server hash equal to the local hash by uploading the same
        # file as the source: FakeSaveApi.download_save copies uploaded_files.
        fake.saves[100] = ss
        fake.uploaded_files[100] = str(save_path)

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="keep_local")

        assert result["success"] is True
        assert result["action"] == "keep_local"
        assert not any(c[0] == "upload_save" for c in fake.call_log)

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 100
        assert file_state["last_sync_hash"] == local_hash

    @pytest.mark.asyncio
    async def test_resolve_keep_local_hash_mismatch_uploads_put(self, tmp_path):
        """Local differs from server content → PUT against existing save id."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-edited")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        # Server has different content uploaded — hash will not match.
        other = tmp_path / "other.bin"
        other.write_bytes(b"server-flavor")
        fake.saves[100] = ss
        fake.uploaded_files[100] = str(other)

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="keep_local")

        assert result["success"] is True
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT — save_id was passed
        assert upload_calls[0][2]["save_id"] == 100

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["last_sync_hash"] == local_hash

    @pytest.mark.asyncio
    async def test_resolve_use_server_downloads_and_persists(self, tmp_path):
        """use_server downloads server, overwrites local, updates state."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-stale")

        # Server has different content
        server_content = tmp_path / "server-content.bin"
        server_content.write_bytes(b"server-truth")
        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss
        fake.uploaded_files[100] = str(server_content)

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="use_server")

        assert result["success"] is True
        # Local file overwritten with server content
        assert save_path.read_bytes() == b"server-truth"
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 100
        assert file_state["last_sync_hash"] == _file_md5(str(save_path))

    @pytest.mark.asyncio
    async def test_resolve_invalid_action_returns_error(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="foo")

        assert result["success"] is False
        assert "invalid" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_resolve_rom_not_installed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)

        result = await svc.resolve_sync_conflict(rom_id=999, filename="pokemon.srm", action="keep_local")

        assert result["success"] is False
        assert result["message"]

    @pytest.mark.asyncio
    async def test_resolve_server_fetch_failure(self, tmp_path):
        """When list_saves raises, return failure without mutating state."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"x")

        # Pre-populate state to assert it stays untouched.
        original_state = {
            "files": {
                "pokemon.srm": {"tracked_save_id": 100, "last_sync_hash": "abc"},
            }
        }
        svc._save_sync_state["saves"]["42"] = original_state

        fake.fail_on_next(RommApiError("network"))

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="keep_local")

        assert result["success"] is False
        assert "Failed to fetch saves" in result["message"]
        # State left as-is — no mutation
        assert svc._save_sync_state["saves"]["42"] == original_state

    @pytest.mark.asyncio
    async def test_resolve_no_server_saves_in_slot(self, tmp_path):
        """Empty slot post-fetch returns success=False with a clear message.

        Implementation note: ``resolve_sync_conflict`` reaches the slot-empty
        branch via ``_filter_server_saves_to_slot`` and returns
        ``{"success": False, "message": "No server save in active slot"}``.
        """
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="keep_local")

        assert result["success"] is False
        assert "no server save" in result["message"].lower()


# ---------------------------------------------------------------------------
# Path-traversal defense (#224)
# ---------------------------------------------------------------------------


class TestPathTraversalDefense:
    """Defense in depth against malicious filenames at the two choke points.

    1. Server-supplied ``file_extension`` flowing through ``_local_save_target``.
    2. Frontend-supplied ``filename`` arriving at ``resolve_sync_conflict``.
    """

    def test_local_save_target_strips_traversal_in_extension(self, caplog):
        """A malicious ``file_extension`` cannot produce a path-escape filename."""
        from services.saves._helpers import _local_save_target

        with caplog.at_level(logging.WARNING):
            target = _local_save_target({"file_extension": "../etc/passwd"}, "pokemon")
        # Sanitization reduces to a simple basename — no separators, no parent refs.
        assert "/" not in target
        assert ".." not in target.split(".")
        assert os.path.basename(target) == target
        # The strip-and-warn path must log a warning identifying the sanitized field.
        assert any("Sanitized" in rec.message and "file_extension" in rec.message for rec in caplog.records)

    def test_local_save_target_happy_path_unchanged(self):
        """Clean ``file_extension`` produces ``<rom_name>.<ext>`` unchanged."""
        from services.saves._helpers import _local_save_target

        assert _local_save_target({"file_extension": "srm"}, "pokemon") == "pokemon.srm"

    def test_local_save_target_falls_back_to_srm_on_unusable_ext(self, caplog):
        """When the server's extension produces an empty/dot-only name, fall back to ``srm``."""
        from services.saves._helpers import _local_save_target

        with caplog.at_level(logging.WARNING):
            # An ``ext`` that drives the basename to ``""`` after sanitization
            # (e.g. trailing separator) — the helper degrades to ``"srm"``.
            target = _local_save_target({"file_extension": "evil/"}, "pokemon")
        # Either the sanitized basename or the safe default — never traversal.
        assert "/" not in target
        assert target.endswith(".srm") or target == "pokemon.srm"
        # The fallback path is the only signal of a glitched server extension —
        # assert it actually fires so a future refactor can't drop it silently.
        assert any("invalid" in rec.message.lower() and "file_extension" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_resolve_sync_conflict_rejects_traversal_filename(self, tmp_path, caplog):
        """Frontend-supplied traversal filename is rejected before any I/O."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local data")

        # Snapshot files outside saves_dir to assert nothing got written there.
        outside = tmp_path / "outside.txt"

        with caplog.at_level(logging.WARNING):
            result = await svc.resolve_sync_conflict(
                rom_id=42,
                filename="../../etc/passwd",
                action="keep_local",
            )

        assert result["success"] is False
        assert "invalid" in result["message"].lower()
        # No I/O against the server (no list_saves, no upload_save).
        assert not any(c[0] == "list_saves" for c in fake.call_log)
        assert not any(c[0] == "upload_save" for c in fake.call_log)
        # Nothing written outside saves_dir.
        assert not outside.exists()
        assert not (tmp_path / "etc").exists()
        # A warning was logged identifying the rejection.
        assert any("rejected" in rec.message.lower() and "filename" in rec.message.lower() for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_resolve_sync_conflict_rejects_null_byte_filename(self, tmp_path):
        """NUL byte in filename is rejected with the same shape."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon\x00.srm",
            action="keep_local",
        )

        assert result["success"] is False
        assert "invalid" in result["message"].lower()


# ---------------------------------------------------------------------------
# Per-rom lock serialization
# ---------------------------------------------------------------------------


class TestPerRomLockSerialization:
    @pytest.mark.asyncio
    async def test_per_rom_lock_serializes_concurrent_sync(self, tmp_path):
        """Two concurrent sync_rom_saves calls on the same rom must not interleave."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local data")

        # Spy timing on _sync_rom_saves entry/exit. The lock is held in the
        # async wrapper around run_in_executor, so the inner call's
        # entry/exit windows for two concurrent invocations must not overlap.
        events: list[tuple[str, float]] = []
        original = svc._sync_rom_saves

        def wrapped(rom_id: int):
            events.append(("enter", time.time()))
            # Sleep to ensure overlap is *possible* if the lock is broken.
            time.sleep(0.05)
            try:
                return original(rom_id)
            finally:
                events.append(("exit", time.time()))

        svc._sync_rom_saves = wrapped  # type: ignore[method-assign]

        await asyncio.gather(svc.sync_rom_saves(42), svc.sync_rom_saves(42))

        # Expect strictly serialized: enter, exit, enter, exit.
        kinds = [k for k, _ts in events]
        assert kinds == ["enter", "exit", "enter", "exit"], events

    @pytest.mark.asyncio
    async def test_per_rom_lock_does_not_block_different_rom_ids(self, tmp_path):
        """Concurrent syncs on different rom_ids run in parallel."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"a")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"b")

        events: list[tuple[int, str, float]] = []
        original = svc._sync_rom_saves

        def wrapped(rom_id: int):
            events.append((rom_id, "enter", time.time()))
            time.sleep(0.05)
            try:
                return original(rom_id)
            finally:
                events.append((rom_id, "exit", time.time()))

        svc._sync_rom_saves = wrapped  # type: ignore[method-assign]

        await asyncio.gather(svc.sync_rom_saves(1), svc.sync_rom_saves(2))

        # Both enters must happen before either exit (proves overlap).
        order = [(rid, kind) for rid, kind, _ts in events]
        enters = [i for i, e in enumerate(order) if e[1] == "enter"]
        exits = [i for i, e in enumerate(order) if e[1] == "exit"]
        assert min(exits) > max(enters), order
