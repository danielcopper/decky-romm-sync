"""Tests for SaveService with FakeSaveApi (no HTTP, no mocking)."""

import asyncio
import hashlib
import logging
import os
import time
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from fakes.fake_save_api import FakeSaveApi

from lib.errors import RommApiError
from services.saves import SaveService

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


def make_service(tmp_path, fake_api=None, *, emit=None, **overrides) -> tuple["SaveService", "FakeSaveApi"]:
    """Create a SaveService with sensible defaults for testing."""
    fake: FakeSaveApi = fake_api or FakeSaveApi()
    defaults: dict[str, Any] = dict(
        romm_api=fake,
        retry=_make_retry(),
        settings={"log_level": "debug"},
        state={"shortcut_registry": {}, "installed_roms": {}},
        save_sync_state=SaveService.make_default_state(),
        loop=asyncio.get_event_loop(),
        logger=logging.getLogger("test"),
        runtime_dir=str(tmp_path),
        get_saves_path=lambda: str(tmp_path / "saves"),
        get_roms_path=lambda: str(tmp_path / "retrodeck" / "roms"),
        get_active_core=lambda system_name, rom_filename=None: (None, None),
        plugin_version="0.14.0",
        emit=emit,
    )
    defaults.update(overrides)
    svc = SaveService(**defaults)
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
):
    result = {
        "id": save_id,
        "rom_id": rom_id,
        "file_name": filename,
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
        assert svc._save_sync_state["settings"]["conflict_mode"] == "ask_me"
        assert svc._save_sync_state["saves"] == {}

    def test_init_state_preserves_existing(self, tmp_path):
        state = SaveService.make_default_state()
        state["device_id"] = "existing-id"
        svc, _ = make_service(tmp_path, save_sync_state=state)
        assert svc._save_sync_state["device_id"] == "existing-id"

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

        result = svc.ensure_device_registered()
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

        result = svc.ensure_device_registered()
        assert result["device_id"] == "existing"
        assert result["device_name"] == "deck"

    @pytest.mark.asyncio
    async def test_disabled_returns_failure(self, tmp_path):
        svc, _ = make_service(tmp_path)
        # save_sync_enabled defaults to False
        result = svc.ensure_device_registered()
        assert result["success"] is False
        assert result.get("disabled") is True


# ---------------------------------------------------------------------------
# TestDeviceRegistrationV47
# ---------------------------------------------------------------------------


class TestDeviceRegistrationV47:
    def test_registers_with_server_on_v47(self, tmp_path):
        """v4.7: calls register_device and stores server_device_id."""
        fake = FakeSaveApi()
        fake._supports_device_sync = True
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        result = svc.ensure_device_registered()
        assert result["success"] is True
        assert result.get("server_device_id") is not None
        assert svc._save_sync_state["server_device_id"] == result["server_device_id"]
        # Verify register_device was called
        reg_calls = [c for c in fake.call_log if c[0] == "register_device"]
        assert len(reg_calls) == 1
        assert reg_calls[0][1][0]  # name (hostname)
        assert reg_calls[0][1][1] == "linux"  # platform
        assert reg_calls[0][1][2] == "decky-romm-sync"  # client

    def test_falls_back_to_local_on_server_failure(self, tmp_path):
        """v4.7: if register_device fails, falls back to local UUID."""
        fake = FakeSaveApi()
        fake._supports_device_sync = True
        fake.fail_on_next(Exception("server error"))
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        result = svc.ensure_device_registered()
        assert result["success"] is True
        assert result["device_id"]  # got a local UUID
        assert result.get("server_device_id") is None  # no server registration
        assert svc._save_sync_state.get("server_device_id") is None

    def test_v46_uses_local_uuid(self, tmp_path):
        """v4.6: generates local UUID without server contact."""
        svc, fake = make_service(tmp_path)  # default: supports_device_sync=False
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        result = svc.ensure_device_registered()
        assert result["success"] is True
        assert result["device_id"]
        assert result.get("server_device_id") is None
        # No register_device call
        reg_calls = [c for c in fake.call_log if c[0] == "register_device"]
        assert len(reg_calls) == 0

    def test_returns_existing_with_server_device_id(self, tmp_path):
        """If already registered, returns existing IDs including server_device_id."""
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "existing-id"
        svc._save_sync_state["device_name"] = "deck"
        svc._save_sync_state["server_device_id"] = "server-id-123"

        result = svc.ensure_device_registered()
        assert result["device_id"] == "existing-id"
        assert result.get("server_device_id") == "server-id-123"

    def test_upgrades_local_uuid_to_server_on_v47(self, tmp_path):
        """Local-only UUID gets upgraded to server registration when v4.7 becomes available."""
        fake = FakeSaveApi()
        fake._supports_device_sync = True
        svc, _ = make_service(tmp_path, fake_api=fake)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        # Simulate existing local-only UUID (from v4.6 or failed v4.7 registration)
        svc._save_sync_state["device_id"] = "local-only-uuid"
        svc._save_sync_state["device_name"] = "deck"
        svc._save_sync_state["server_device_id"] = None

        result = svc.ensure_device_registered()
        assert result["success"] is True
        assert result.get("server_device_id") is not None
        assert svc._save_sync_state["server_device_id"] is not None
        # register_device was called
        reg_calls = [c for c in fake.call_log if c[0] == "register_device"]
        assert len(reg_calls) == 1


# ---------------------------------------------------------------------------
# TestConflictDetection
# ---------------------------------------------------------------------------


class TestConflictDetection:
    def test_skip_when_unchanged(self, tmp_path):
        svc, _ = make_service(tmp_path)
        content = b"save data"
        local_hash = hashlib.md5(content).hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        server = _server_save()
        result = svc._detect_conflict(42, "pokemon.srm", local_hash, server)
        assert result == "skip"

    def test_upload_when_local_changed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        old_hash = hashlib.md5(b"old").hexdigest()
        new_hash = hashlib.md5(b"new data").hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": old_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        server = _server_save()
        result = svc._detect_conflict(42, "pokemon.srm", new_hash, server)
        assert result == "upload"

    def test_download_when_server_changed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        local_hash = hashlib.md5(b"save data").hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2026-02-17T04:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        # Server has a different updated_at, and downloading it yields a different hash
        server = _server_save(updated_at="2026-02-17T08:00:00Z", file_size_bytes=2048)
        # The slow path will download + hash. The fake will write default content.
        # We need the downloaded hash to differ from last_sync_hash.
        # Since FakeSaveApi.download_save writes 1024 zero bytes and local_hash is md5("save data"),
        # the server hash will naturally differ.
        result = svc._detect_conflict(42, "pokemon.srm", local_hash, server)
        assert result == "download"

    def test_conflict_when_both_changed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        old_hash = hashlib.md5(b"baseline").hexdigest()
        new_local = hashlib.md5(b"local edit").hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": old_hash,
                    "last_sync_server_updated_at": "2026-02-17T04:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        server = _server_save(updated_at="2026-02-17T08:00:00Z", file_size_bytes=2048)
        result = svc._detect_conflict(42, "pokemon.srm", new_local, server)
        assert result == "conflict"

    def test_never_synced_same_hash_skips(self, tmp_path):
        """When never synced but local and server have same content -> skip."""
        svc, _ = make_service(tmp_path)
        # FakeSaveApi.download_save writes 1024 zero bytes
        content = b"\x00" * 1024
        local_hash = hashlib.md5(content).hexdigest()

        server = _server_save()
        result = svc._detect_conflict(42, "pokemon.srm", local_hash, server)
        assert result == "skip"

    def test_never_synced_different_hash_conflicts(self, tmp_path):
        """When never synced and hashes differ -> conflict."""
        svc, _ = make_service(tmp_path)
        local_hash = hashlib.md5(b"different content").hexdigest()

        server = _server_save()
        result = svc._detect_conflict(42, "pokemon.srm", local_hash, server)
        assert result == "conflict"

    def test_first_sync_download_fails(self, tmp_path):
        """First sync (no snapshot), server download fails -> conflict."""
        svc, fake = make_service(tmp_path)
        local_hash = hashlib.md5(b"local data").hexdigest()

        server = _server_save()
        fake.fail_on_next(Exception("download failed"))
        result = svc._detect_conflict(42, "pokemon.srm", local_hash, server)
        assert result == "conflict"

    def test_no_local_file_downloads(self, tmp_path):
        """No local file (local_hash=None), server has save -> download."""
        svc, _ = make_service(tmp_path)

        server = _server_save()
        result = svc._detect_conflict(42, "pokemon.srm", None, server)
        assert result == "download"

    def test_server_size_change_fast_path(self, tmp_path):
        """Same timestamp but different size -> detects server changed."""
        svc, _ = make_service(tmp_path)
        local_hash = hashlib.md5(b"save data").hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        # Same timestamp but different size -> fast path detects change
        server = _server_save(updated_at="2026-02-17T06:00:00Z", file_size_bytes=2048)
        result = svc._detect_conflict(42, "pokemon.srm", local_hash, server)
        # local unchanged, server changed -> download
        assert result == "download"


# ---------------------------------------------------------------------------
# TestConflictDetectionFalseAlarm
# ---------------------------------------------------------------------------


class TestConflictDetectionFalseAlarm:
    """Timestamp changed but content identical -> false alarm detection."""

    def test_timestamp_changed_content_identical_skips(self, tmp_path):
        """Server timestamp changed but content hash matches baseline -> skip."""
        svc, _ = make_service(tmp_path)
        # FakeSaveApi.download_save writes 1024 zero bytes by default
        baseline_hash = hashlib.md5(b"\x00" * 1024).hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": baseline_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        # Different timestamp triggers slow path; download yields same hash as baseline
        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        result = svc._detect_conflict(42, "pokemon.srm", baseline_hash, server)
        assert result == "skip"

    def test_false_alarm_updates_stored_metadata(self, tmp_path):
        """After false alarm, stored server_updated_at and server_size are updated."""
        svc, _ = make_service(tmp_path)
        baseline_hash = hashlib.md5(b"\x00" * 1024).hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": baseline_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        server = _server_save(updated_at="2026-02-17T12:00:00Z", file_size_bytes=2048)
        svc._detect_conflict(42, "pokemon.srm", baseline_hash, server)

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["last_sync_server_updated_at"] == "2026-02-17T12:00:00Z"
        assert file_state["last_sync_server_size"] == 2048

    def test_local_changed_server_content_unchanged_uploads(self, tmp_path):
        """Local changed + server timestamp changed but server content unchanged -> upload."""
        svc, _ = make_service(tmp_path)
        baseline_hash = hashlib.md5(b"\x00" * 1024).hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": baseline_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        new_local_hash = hashlib.md5(b"new local data").hexdigest()
        result = svc._detect_conflict(42, "pokemon.srm", new_local_hash, server)
        assert result == "upload"

    def test_no_stored_timestamp_triggers_slow_path(self, tmp_path):
        """No stored server_updated_at -> triggers slow path (download + hash)."""
        svc, _ = make_service(tmp_path)
        baseline_hash = hashlib.md5(b"\x00" * 1024).hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": baseline_hash,
                    # No last_sync_server_updated_at
                }
            }
        }

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        # Slow path downloads save, hash matches baseline -> skip
        result = svc._detect_conflict(42, "pokemon.srm", baseline_hash, server)
        assert result == "skip"

    def test_slow_path_download_fails_server_unchanged(self, tmp_path):
        """_get_server_save_hash returns None (download failed) -> treated as server unchanged."""
        svc, fake = make_service(tmp_path)
        baseline_hash = hashlib.md5(b"baseline").hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": baseline_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        fake.fail_on_next(Exception("download failed"))
        # Local unchanged + server hash None -> server unchanged -> skip
        result = svc._detect_conflict(42, "pokemon.srm", baseline_hash, server)
        assert result == "skip"

    def test_slow_path_download_fails_local_changed(self, tmp_path):
        """_get_server_save_hash returns None, local changed -> upload."""
        svc, fake = make_service(tmp_path)
        baseline_hash = hashlib.md5(b"baseline").hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": baseline_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        fake.fail_on_next(Exception("download failed"))
        new_local_hash = hashlib.md5(b"new local data").hexdigest()
        result = svc._detect_conflict(42, "pokemon.srm", new_local_hash, server)
        assert result == "upload"

    def test_fast_path_timestamp_and_size_match(self, tmp_path):
        """Both timestamp and size match stored -> skip."""
        svc, _ = make_service(tmp_path)
        local_hash = hashlib.md5(b"save data").hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            }
        }

        server = _server_save(updated_at="2026-02-17T06:00:00Z", file_size_bytes=1024)
        result = svc._detect_conflict(42, "pokemon.srm", local_hash, server)
        assert result == "skip"

    def test_fast_path_stored_size_none(self, tmp_path):
        """stored_size is None (legacy), timestamp matches -> skip (size check skipped)."""
        svc, _ = make_service(tmp_path)
        local_hash = hashlib.md5(b"save data").hexdigest()

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": None,
                }
            }
        }

        # Different file size but stored_size is None -> size check skipped -> unchanged
        server = _server_save(updated_at="2026-02-17T06:00:00Z", file_size_bytes=2048)
        result = svc._detect_conflict(42, "pokemon.srm", local_hash, server)
        assert result == "skip"


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

    def test_matching_saves_skip(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        ss = _server_save()
        fake.saves[100] = ss

        # Set sync state to match
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": ss["updated_at"],
                    "last_sync_server_size": ss["file_size_bytes"],
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)
        assert synced == 0
        assert errors == []
        assert conflicts == []

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
# TestResolveConflict
# ---------------------------------------------------------------------------


class TestResolveConflict:
    @pytest.mark.asyncio
    async def test_download_resolution(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.resolve_conflict(
            rom_id=42,
            filename="pokemon.srm",
            resolution="download",
            server_save_id=100,
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_upload_resolution(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.resolve_conflict(
            rom_id=42,
            filename="pokemon.srm",
            resolution="upload",
            server_save_id=100,
            local_path=str(save_path),
        )
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_invalid_resolution(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.resolve_conflict(rom_id=42, filename="x.srm", resolution="invalid", server_save_id=100)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_missing_server_save_id(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.resolve_conflict(rom_id=42, filename="x.srm", resolution="download")
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_rom_not_installed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.resolve_conflict(rom_id=999, filename="x.srm", resolution="download", server_save_id=100)
        assert result["success"] is False


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
    async def test_get_save_status_conflicts_populated_when_conflict_detected(self, tmp_path):
        """When local and server both exist but never synced, conflicts list is populated."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        # Create local file with content different from server (server writes zeros)
        _create_save(tmp_path, content=b"different content")

        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.get_save_status(42)
        assert "conflicts" in result
        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["filename"] == "pokemon.srm"
        assert result["conflicts"][0]["rom_id"] == 42

    @pytest.mark.asyncio
    async def test_get_save_status_conflicts_has_required_fields(self, tmp_path):
        """Conflict entries include all fields needed for resolution."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"different content")

        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.get_save_status(42)
        assert len(result["conflicts"]) == 1
        conflict = result["conflicts"][0]
        assert conflict["rom_id"] == 42
        assert conflict["filename"] == "pokemon.srm"
        assert conflict["local_path"] is not None
        assert conflict["server_save_id"] == 100
        assert conflict["server_updated_at"] == "2026-02-17T06:00:00Z"
        assert "created_at" in conflict

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
        assert settings["conflict_mode"] == "ask_me"
        assert settings["save_sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_update_settings(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.update_save_sync_settings(
            {
                "save_sync_enabled": True,
                "conflict_mode": "newest_wins",
            }
        )
        assert result["success"] is True
        assert result["settings"]["save_sync_enabled"] is True
        assert result["settings"]["conflict_mode"] == "newest_wins"

    @pytest.mark.asyncio
    async def test_invalid_mode_ignored(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc.update_save_sync_settings({"conflict_mode": "invalid_mode"})
        settings = svc.get_save_sync_settings()
        assert settings["conflict_mode"] == "ask_me"

    @pytest.mark.asyncio
    async def test_unknown_key_ignored(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = svc.update_save_sync_settings({"unknown_key": "value"})
        assert result["success"] is True
        assert "unknown_key" not in result["settings"]

    @pytest.mark.asyncio
    async def test_clock_skew_clamped(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc.update_save_sync_settings({"clock_skew_tolerance_sec": -10})
        settings = svc.get_save_sync_settings()
        assert settings["clock_skew_tolerance_sec"] == 0


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
        assert "42" not in svc._save_sync_state["saves"]

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
    async def test_delete_platform_saves_other_platform_untouched(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1")
        snes_save = _create_save(tmp_path, system="snes", rom_name="game2")

        svc.delete_platform_saves("gba")
        assert snes_save.exists()


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
        f = tmp_path / "test.srm"
        content = b"Hello, save file!"
        f.write_bytes(content)

        assert SaveService._file_md5(str(f)) == hashlib.md5(content).hexdigest()

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.srm"
        f.write_bytes(b"")

        assert SaveService._file_md5(str(f)) == hashlib.md5(b"").hexdigest()

    def test_large_file_chunked(self, tmp_path):
        f = tmp_path / "large.srm"
        content = os.urandom(2 * 1024 * 1024)
        f.write_bytes(content)

        assert SaveService._file_md5(str(f)) == hashlib.md5(content).hexdigest()

    def test_permission_error(self, tmp_path):
        f = tmp_path / "locked.srm"
        f.write_bytes(b"data")
        f.chmod(0o000)

        try:
            with pytest.raises(PermissionError):
                SaveService._file_md5(str(f))
        finally:
            f.chmod(0o644)


# ---------------------------------------------------------------------------
# TestResolveConflictByMode
# ---------------------------------------------------------------------------


class TestResolveConflictByMode:
    """Tests for _resolve_conflict_by_mode."""

    def test_always_upload(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "always_upload"
        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        result = svc._resolve_conflict_by_mode(time.time() - 7200, server)

        assert result == "upload"

    def test_always_download(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "always_download"
        server = _server_save(updated_at="2026-02-17T10:00:00Z")

        result = svc._resolve_conflict_by_mode(time.time(), server)

        assert result == "download"

    def test_ask_me(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "ask_me"
        server = _server_save()

        result = svc._resolve_conflict_by_mode(time.time(), server)

        assert result == "ask"

    def test_newest_wins_local_newer(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        server = _server_save(updated_at="2026-02-17T10:00:00Z")
        local_mtime = datetime(2026, 2, 17, 14, 0, 0, tzinfo=UTC).timestamp()

        result = svc._resolve_conflict_by_mode(local_mtime, server)

        assert result == "upload"

    def test_newest_wins_server_newer(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        server = _server_save(updated_at="2026-02-17T14:00:00Z")
        local_mtime = datetime(2026, 2, 17, 10, 0, 0, tzinfo=UTC).timestamp()

        result = svc._resolve_conflict_by_mode(local_mtime, server)

        assert result == "download"

    def test_newest_wins_within_clock_skew(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        svc._save_sync_state["settings"]["clock_skew_tolerance_sec"] = 60
        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        local_mtime = datetime(2026, 2, 17, 12, 0, 30, tzinfo=UTC).timestamp()

        result = svc._resolve_conflict_by_mode(local_mtime, server)

        assert result == "ask"


# ---------------------------------------------------------------------------
# TestClockSkewBoundary
# ---------------------------------------------------------------------------


class TestClockSkewBoundary:
    """Boundary tests for clock skew tolerance in newest_wins mode."""

    def test_exactly_at_tolerance_boundary_asks(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        svc._save_sync_state["settings"]["clock_skew_tolerance_sec"] = 60

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        local_mtime = datetime(2026, 2, 17, 12, 1, 0, tzinfo=UTC).timestamp()

        result = svc._resolve_conflict_by_mode(local_mtime, server)

        assert result == "ask"

    def test_one_second_beyond_tolerance_resolves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        svc._save_sync_state["settings"]["clock_skew_tolerance_sec"] = 60

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        local_mtime = datetime(2026, 2, 17, 12, 1, 1, tzinfo=UTC).timestamp()

        result = svc._resolve_conflict_by_mode(local_mtime, server)

        assert result == "upload"

    def test_zero_tolerance_resolves_any_difference(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        svc._save_sync_state["settings"]["clock_skew_tolerance_sec"] = 0

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        local_mtime = datetime(2026, 2, 17, 12, 0, 1, tzinfo=UTC).timestamp()

        result = svc._resolve_conflict_by_mode(local_mtime, server)

        assert result == "upload"

    def test_invalid_server_timestamp_asks(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "newest_wins"

        server = _server_save(updated_at="not-a-timestamp")

        result = svc._resolve_conflict_by_mode(time.time(), server)

        assert result == "ask"

    def test_empty_server_timestamp_asks(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["conflict_mode"] = "newest_wins"

        server = _server_save(updated_at="")

        result = svc._resolve_conflict_by_mode(time.time(), server)

        assert result == "ask"


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
        assert entry["last_sync_hash"] == SaveService._file_md5(str(save_file))
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

    def test_v46_does_not_pass_device_id(self, tmp_path):
        """v4.6: no device_id or slot passed to API calls."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "local-uuid"
        # No server_device_id set
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        svc._sync_rom_saves(42)

        list_calls = [c for c in fake.call_log if c[0] == "list_saves"]
        assert list_calls[0][2]["device_id"] is None

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        if upload_calls:
            assert upload_calls[0][2]["device_id"] is None
            assert upload_calls[0][2]["slot"] is None

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
        val, skip = svc._sanitize_setting("default_slot", "", set())
        assert val is None
        assert skip is False

    def test_none_value_passes_through(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", None, set())
        assert val is None
        assert skip is False

    def test_whitespace_only_becomes_none(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", "   ", set())
        assert val is None
        assert skip is False

    def test_nonempty_string_trimmed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        val, skip = svc._sanitize_setting("default_slot", "  desktop  ", set())
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
    """Tests for get_save_slots and set_game_slot."""

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
    async def test_get_save_slots_disabled(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.get_save_slots(123)
        assert result["success"] is False

    def test_set_game_slot(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["saves"] = {
            "123": {"system": "gba", "active_slot": "default", "files": {}},
        }
        result = svc.set_game_slot(123, "desktop")
        assert result["success"] is True
        assert svc._save_sync_state["saves"]["123"]["active_slot"] == "desktop"

    def test_set_game_slot_creates_entry(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        result = svc.set_game_slot(456, "my-slot")
        assert result["success"] is True
        assert svc._save_sync_state["saves"]["456"]["active_slot"] == "my-slot"

    def test_set_game_slot_empty_sets_none(self, tmp_path):
        """Empty string sets active_slot to None (legacy mode)."""
        svc, _ = make_service(tmp_path)
        result = svc.set_game_slot(123, "")
        assert result["success"] is True
        assert result["active_slot"] is None
        assert svc._save_sync_state["saves"]["123"]["active_slot"] is None

    @pytest.mark.asyncio
    async def test_set_game_slot_triggers_background_check(self, tmp_path):
        """set_game_slot fires a background save status check task."""
        emitted = []

        async def fake_emit(event, *args):
            emitted.append((event, args))

        svc, _ = make_service(tmp_path, emit=fake_emit)
        _install_rom(svc, tmp_path)

        svc.set_game_slot(42, "slot1")

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
    async def test_confirm_with_migration(self, tmp_path):
        """Migrate: re-upload to new slot, delete old."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        svc._save_sync_state["server_device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        # Old save on server with slot=None (legacy)
        fake.saves[1] = _server_save(save_id=1, slot=None)

        result = await svc.confirm_slot_choice(42, "default", migrate_from_slot=None)
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
        """Migration failure should still confirm the slot but report the issue."""
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

        result = await svc.confirm_slot_choice(42, "default", migrate_from_slot=None)
        assert result["success"] is True
        assert "migration failed" in result["message"].lower()
        # Slot is still confirmed despite migration failure
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

    def test_upload_uses_tracked_save_id_for_put(self, tmp_path):
        """When tracked_save_id exists, upload does PUT (update) not POST (new)."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"updated save data")

        # Server has save with timestamp filename (different from local)
        fake.saves[42] = {
            "id": 42,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-24_15-18-50].srm",
            "updated_at": "2026-03-20T10:00:00",
            "file_size_bytes": 100,
            "emulator": "retroarch",
            "download_path": "/saves/pokemon [2026-03-24_15-18-50].srm",
        }

        # State tracks this save by ID
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "last_synced_core": "mgba_libretro",
            "last_sync_check_at": "2026-03-20T10:00:00",
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": "old_hash",
                    "last_sync_at": "2026-03-20T10:00:00",
                    "last_sync_server_updated_at": "2026-03-20T10:00:00",
                    "last_sync_server_save_id": 42,
                    "last_sync_server_size": 100,
                    "local_mtime_at_last_sync": "2026-03-20T10:00:00",
                    "tracked_save_id": 42,
                },
            },
        }

        _synced, errors, _conflicts = svc._sync_rom_saves(42)
        assert len(errors) == 0

        # Should have done PUT (update save_id=42) not POST (new save)
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) >= 1
        # upload_save logs (name, (rom_id, file_path, emulator), {save_id: ..., ...})
        call_kwargs = upload_calls[0][2]
        assert call_kwargs.get("save_id") == 42

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
        download_calls = [c for c in fake.call_log if c[0] == "download_save"]
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

    def test_fallback_matches_newest_server_save_when_no_tracked_id(self, tmp_path):
        """When tracked_save_id is missing, match the newest server save in active slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["settings"]["conflict_mode"] = "always_upload"
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local save data")

        # Server has multiple saves with timestamp names (from previous stacking)
        fake.saves[10] = {
            "id": 10,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-24_10-00-00].srm",
            "updated_at": "2026-03-24T10:00:00",
            "file_size_bytes": 100,
            "emulator": "retroarch",
            "slot": "default",
            "download_path": "/saves/pokemon [2026-03-24_10-00-00].srm",
        }
        fake.saves[20] = {
            "id": 20,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-24_15-00-00].srm",
            "updated_at": "2026-03-24T15:00:00",
            "file_size_bytes": 200,
            "emulator": "retroarch",
            "slot": "default",
            "download_path": "/saves/pokemon [2026-03-24_15-00-00].srm",
        }

        # State has active_slot but NO tracked_save_id (state was reset)
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {},
        }

        _synced, errors, _conflicts = svc._sync_rom_saves(42)
        assert len(errors) == 0

        # Should have done PUT (update save_id=20, the newest) not POST (new save)
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) >= 1
        call_kwargs = upload_calls[0][2]
        assert call_kwargs.get("save_id") == 20

        # tracked_save_id should now be persisted in state
        files_state = svc._save_sync_state["saves"]["42"]["files"]
        assert "pokemon.srm" in files_state
        assert files_state["pokemon.srm"].get("tracked_save_id") == 20

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

    def test_fallback_skips_already_matched_server_saves(self, tmp_path):
        """Fallback should not match a server save already matched by another local file."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["settings"]["conflict_mode"] = "always_upload"
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local srm", ext=".srm")
        _create_save(tmp_path, content=b"local rtc", ext=".rtc")

        # One server save matched by filename, one with timestamp name
        fake.saves[10] = {
            "id": 10,
            "rom_id": 42,
            "file_name": "pokemon.rtc",
            "updated_at": "2026-03-24T10:00:00",
            "file_size_bytes": 100,
            "emulator": "retroarch",
            "slot": "default",
            "download_path": "/saves/pokemon.rtc",
        }
        fake.saves[20] = {
            "id": 20,
            "rom_id": 42,
            "file_name": "pokemon [ts].srm",
            "updated_at": "2026-03-24T15:00:00",
            "file_size_bytes": 200,
            "emulator": "retroarch",
            "slot": "default",
            "download_path": "/saves/pokemon [ts].srm",
        }

        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {},
        }

        _synced, errors, _conflicts = svc._sync_rom_saves(42)
        assert len(errors) == 0

        # pokemon.rtc should match by filename (id=10)
        # pokemon.srm should fallback to newest unmatched (id=20)
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        save_ids = {c[2].get("save_id") for c in upload_calls}
        assert 10 in save_ids  # rtc matched by filename
        assert 20 in save_ids  # srm matched by fallback

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
        download_calls = [c for c in fake.call_log if c[0] == "download_save"]
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

    def test_older_versions_skipped_during_sync(self, tmp_path):
        """After uploading to tracked id=18, older id=16/17 in same slot must not download."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"current save")

        # Server has 3 versions, all in slot=default
        for sid, ts, upd in [
            (16, "15-18-50", "2026-03-24T15:18:50"),
            (17, "15-19-15", "2026-03-24T15:19:15"),
            (18, "15-19-26", "2026-03-24T15:19:26"),
        ]:
            fake.saves[sid] = _server_save(
                save_id=sid,
                filename=f"pokemon [2026-03-24_{ts}].srm",
                updated_at=upd,
                slot="default",
            )

        local_hash = _file_md5(tmp_path / "saves" / "gba" / "pokemon.srm")
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 18,
                    "last_sync_hash": local_hash,
                    "last_sync_at": "2026-03-24T15:19:26",
                    "last_sync_server_updated_at": "2026-03-24T15:19:26",
                    "last_sync_server_save_id": 18,
                    "last_sync_server_size": 1024,
                    "local_mtime_at_last_sync": "2026-03-24T15:19:26",
                },
            },
        }

        synced, errors, _conflicts = svc._sync_rom_saves(42)
        assert len(errors) == 0
        # Nothing should sync — local matches server (id=18), older versions ignored
        assert synced == 0
        download_calls = [c for c in fake.call_log if c[0] == "download_save"]
        assert len(download_calls) == 0

    def test_newer_unmatched_save_not_skipped(self, tmp_path):
        """If an unmatched server save is NEWER than the matched one, don't skip it."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["device_id"] = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"old save")

        # Matched save is old (id=10)
        fake.saves[10] = _server_save(
            save_id=10,
            filename="pokemon.srm",
            updated_at="2026-03-20T10:00:00",
            slot="default",
        )
        # Unmatched save is NEWER (id=20) — different extension, should not be skipped
        fake.saves[20] = _server_save(
            save_id=20,
            filename="pokemon.rtc",
            updated_at="2026-03-25T10:00:00",
            slot="default",
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
                    "last_sync_at": "2026-03-20T10:00:00",
                    "last_sync_server_updated_at": "2026-03-20T10:00:00",
                    "last_sync_server_save_id": 10,
                    "last_sync_server_size": 1024,
                    "local_mtime_at_last_sync": "2026-03-20T10:00:00",
                },
            },
        }

        _synced, _errors, _conflicts = svc._sync_rom_saves(42)
        # pokemon.rtc (save_id=20) is newer in the same slot — surfaced as newer_in_slot conflict
        newer_conflicts = [c for c in _conflicts if isinstance(c, dict) and c.get("type") == "newer_in_slot"]
        assert len(newer_conflicts) == 1
        assert newer_conflicts[0]["newer_save_id"] == 20

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
        download_calls = [c for c in fake.call_log if c[0] == "download_save"]
        assert len(download_calls) == 0


# ---------------------------------------------------------------------------
# TestNewerInSlotConflict
# ---------------------------------------------------------------------------


class TestNewerInSlotConflict:
    """Tests for newer-in-slot conflict surfacing and resolution."""

    def _setup_tracked_rom(self, svc, fake, tmp_path, local_hash):
        """Set up a tracked ROM with a local save and a tracked server save."""
        _install_rom(svc, tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True

        # Tracked server save (our current save)
        tracked = _server_save(save_id=100, updated_at="2026-03-20T10:00:00Z", slot="default")
        tracked["device_syncs"] = [{"device_id": "our-device", "is_current": True}]
        fake.saves[100] = tracked

        # Set up sync state so local matches tracked
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "slot_confirmed": True,
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": local_hash,
                    "last_sync_at": "2026-03-20T10:00:00Z",
                    "last_sync_server_updated_at": "2026-03-20T10:00:00Z",
                    "last_sync_server_save_id": 100,
                    "last_sync_server_size": 1024,
                },
            },
        }
        return tracked

    def test_newer_in_slot_surfaces_conflict(self, tmp_path):
        """When a newer save from another device exists, a newer_in_slot conflict is surfaced."""
        svc, fake = make_service(tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))
        svc._save_sync_state["server_device_id"] = "our-device"
        self._setup_tracked_rom(svc, fake, tmp_path, local_hash)

        # Newer save from another device
        newer = _server_save(save_id=200, updated_at="2026-03-21T10:00:00Z", slot="default")
        newer["device_syncs"] = [{"device_id": "other-device", "is_current": True}]
        fake.saves[200] = newer

        _synced, _errors, conflicts = svc._sync_rom_saves(42)

        # Should have a newer_in_slot conflict
        newer_conflicts = [c for c in conflicts if isinstance(c, dict) and c.get("type") == "newer_in_slot"]
        assert len(newer_conflicts) == 1
        assert newer_conflicts[0]["newer_save_id"] == 200
        assert newer_conflicts[0]["tracked_save_id"] == 100
        assert newer_conflicts[0]["rom_id"] == 42
        assert newer_conflicts[0]["filename"] == "pokemon.srm"
        assert newer_conflicts[0]["slot"] == "default"

    def test_newer_in_slot_skips_normal_sync(self, tmp_path):
        """When newer_in_slot conflict is surfaced, normal sync is skipped for that file."""
        svc, fake = make_service(tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))
        svc._save_sync_state["server_device_id"] = "our-device"
        self._setup_tracked_rom(svc, fake, tmp_path, local_hash)

        newer = _server_save(save_id=200, updated_at="2026-03-21T10:00:00Z", slot="default")
        newer["device_syncs"] = [{"device_id": "other-device", "is_current": True}]
        fake.saves[200] = newer

        synced, _errors, _conflicts = svc._sync_rom_saves(42)
        assert synced == 0  # No sync happened, conflict surfaced instead

    def test_dismissed_newer_save_id_suppresses_conflict(self, tmp_path):
        """When dismissed_newer_save_id matches, the conflict is not surfaced."""
        svc, fake = make_service(tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))
        svc._save_sync_state["server_device_id"] = "our-device"
        self._setup_tracked_rom(svc, fake, tmp_path, local_hash)

        # Mark save 200 as dismissed
        svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]["dismissed_newer_save_id"] = 200

        newer = _server_save(save_id=200, updated_at="2026-03-21T10:00:00Z", slot="default")
        newer["device_syncs"] = [{"device_id": "other-device", "is_current": True}]
        fake.saves[200] = newer

        _synced, _errors, conflicts = svc._sync_rom_saves(42)
        newer_conflicts = [c for c in conflicts if isinstance(c, dict) and c.get("type") == "newer_in_slot"]
        assert len(newer_conflicts) == 0

    def test_dismissed_does_not_suppress_even_newer_save(self, tmp_path):
        """A dismissed ID does NOT suppress a conflict from an even newer save."""
        svc, fake = make_service(tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))
        svc._save_sync_state["server_device_id"] = "our-device"
        self._setup_tracked_rom(svc, fake, tmp_path, local_hash)

        # Dismissed save 200, but now save 300 is newer
        svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]["dismissed_newer_save_id"] = 200

        newer = _server_save(save_id=300, updated_at="2026-03-22T10:00:00Z", slot="default")
        newer["device_syncs"] = [{"device_id": "other-device", "is_current": True}]
        fake.saves[300] = newer

        _synced, _errors, conflicts = svc._sync_rom_saves(42)
        newer_conflicts = [c for c in conflicts if isinstance(c, dict) and c.get("type") == "newer_in_slot"]
        assert len(newer_conflicts) == 1
        assert newer_conflicts[0]["newer_save_id"] == 300

    def test_no_conflict_when_newer_save_is_from_our_device(self, tmp_path):
        """No conflict when the newer save is from our own device."""
        svc, fake = make_service(tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))
        svc._save_sync_state["server_device_id"] = "our-device"
        self._setup_tracked_rom(svc, fake, tmp_path, local_hash)

        # Newer save from OUR device (should not trigger conflict)
        newer = _server_save(save_id=200, updated_at="2026-03-21T10:00:00Z", slot="default")
        newer["device_syncs"] = [{"device_id": "our-device", "is_current": True}]
        fake.saves[200] = newer

        _synced, _errors, conflicts = svc._sync_rom_saves(42)
        newer_conflicts = [c for c in conflicts if isinstance(c, dict) and c.get("type") == "newer_in_slot"]
        assert len(newer_conflicts) == 0


class TestResolveNewerInSlot:
    """Tests for resolve_newer_in_slot callable."""

    @pytest.mark.asyncio
    async def test_dismiss_stores_id(self, tmp_path):
        svc, _fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "files": {"pokemon.srm": {"tracked_save_id": 100}},
        }

        result = await svc.resolve_newer_in_slot(42, "pokemon.srm", "dismiss", 200)
        assert result["success"] is True
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["dismissed_newer_save_id"] == 200

    @pytest.mark.asyncio
    async def test_keep_current_no_state_change(self, tmp_path):
        svc, _fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "files": {"pokemon.srm": {"tracked_save_id": 100}},
        }

        result = await svc.resolve_newer_in_slot(42, "pokemon.srm", "keep_current", 200)
        assert result["success"] is True
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert "dismissed_newer_save_id" not in file_state

    @pytest.mark.asyncio
    async def test_use_newer_downloads_save(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        svc._save_sync_state["settings"]["save_sync_enabled"] = True
        svc._save_sync_state["server_device_id"] = "our-device"
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "files": {"pokemon.srm": {"tracked_save_id": 100, "dismissed_newer_save_id": 150}},
        }
        newer = _server_save(save_id=200, updated_at="2026-03-21T10:00:00Z", slot="default")
        fake.saves[200] = newer

        result = await svc.resolve_newer_in_slot(42, "pokemon.srm", "use_newer", 200)
        assert result["success"] is True
        # Verify download happened
        download_calls = [c for c in fake.call_log if c[0] == "download_save"]
        assert len(download_calls) == 1
        assert download_calls[0][1][0] == 200
        # dismissed_newer_save_id should be cleared
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert "dismissed_newer_save_id" not in file_state

    @pytest.mark.asyncio
    async def test_use_newer_rom_not_installed(self, tmp_path):
        svc, _fake = make_service(tmp_path)
        svc._save_sync_state["saves"]["999"] = {
            "system": "gba",
            "active_slot": "default",
            "files": {"pokemon.srm": {"tracked_save_id": 100}},
        }

        result = await svc.resolve_newer_in_slot(999, "pokemon.srm", "use_newer", 200)
        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_use_newer_save_not_found_on_server(self, tmp_path):
        svc, _fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        svc._save_sync_state["server_device_id"] = "our-device"
        svc._save_sync_state["saves"]["42"] = {
            "system": "gba",
            "active_slot": "default",
            "files": {"pokemon.srm": {"tracked_save_id": 100}},
        }

        result = await svc.resolve_newer_in_slot(42, "pokemon.srm", "use_newer", 999)
        assert result["success"] is False
        assert "not found" in result["message"].lower()


class TestUpdateFileSyncStateClearsNewerDismissed:
    """Test that _update_file_sync_state clears dismissed_newer_save_id."""

    def test_clears_dismissed_newer_save_id(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        # Pre-populate with dismissed_newer_save_id
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "dismissed_newer_save_id": 200,
                    "tracked_save_id": 100,
                },
            },
            "system": "gba",
            "active_slot": "default",
        }
        server_resp = {"id": 300, "updated_at": "2026-02-17T15:00:00Z"}

        svc._update_file_sync_state("42", "pokemon.srm", server_resp, str(save_file), "gba")

        entry = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert "dismissed_newer_save_id" not in entry
        assert entry.get("tracked_save_id") == 300


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
