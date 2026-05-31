"""Tests for the PersistenceAdapter: locking, version stamping, and load edge cases."""

import json
import logging
import os
import threading

import pytest

from adapters.persistence import (
    _FIRMWARE_CACHE_VERSION,
    _SETTINGS_VERSION,
    DEFAULT_SETTINGS,
    FirmwareCachePersisterAdapter,
    PersistenceAdapter,
)


@pytest.fixture
def logger():
    return logging.getLogger("test_persistence")


@pytest.fixture
def adapter(tmp_path, logger):
    settings_dir = str(tmp_path / "settings")
    runtime_dir = str(tmp_path / "runtime")
    os.makedirs(settings_dir, exist_ok=True)
    os.makedirs(runtime_dir, exist_ok=True)
    return PersistenceAdapter(settings_dir=settings_dir, runtime_dir=runtime_dir, logger=logger)


# ── Locking tests ──────────────────────────────────────────────────────────────


class TestLocking:
    def test_save_settings_creates_lock_file(self, adapter):
        adapter.save_settings({"romm_url": "http://example.com"})
        lock_path = os.path.join(adapter._settings_dir, "settings.json.lock")
        assert os.path.exists(lock_path)

    def test_save_settings_atomic_write(self, adapter):
        data = {"romm_url": "http://example.com", "romm_user": "testuser"}
        adapter.save_settings(data)
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path) as f:
            loaded = json.load(f)
        assert loaded["romm_url"] == "http://example.com"
        assert loaded["romm_user"] == "testuser"

    def test_save_firmware_cache_creates_lock_file(self, adapter):
        adapter.save_firmware_cache({"snes": {"files": []}})
        lock_path = os.path.join(adapter._runtime_dir, "firmware_cache.json.lock")
        assert os.path.exists(lock_path)

    def test_locked_write_concurrent(self, adapter):
        """Two threads writing simultaneously — final file must be valid JSON."""
        results = []
        errors = []

        def write_worker(value):
            try:
                adapter.save_settings({"romm_url": f"http://server{value}.com"})
                results.append(value)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=write_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors during concurrent writes: {errors}"
        assert len(results) == 10

        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path) as f:
            loaded = json.load(f)
        # The file must be valid JSON with the expected shape
        assert "romm_url" in loaded
        assert "version" in loaded


# ── Version stamping on save ───────────────────────────────────────────────────


class TestSettingsSchema:
    """Schema-level expectations for the settings defaults + version stamp."""

    def test_settings_version_is_6(self):
        assert _SETTINGS_VERSION == 6

    def test_default_settings_carry_token_slots(self):
        assert DEFAULT_SETTINGS["romm_api_token"] is None
        assert DEFAULT_SETTINGS["romm_api_token_id"] is None

    def test_load_settings_backfills_token_slots(self, adapter):
        result = adapter.load_settings()
        assert result["romm_api_token"] is None
        assert result["romm_api_token_id"] is None


class TestVersionStampingOnSave:
    def test_save_settings_stamps_version(self, adapter):
        adapter.save_settings({"romm_url": "http://example.com"})
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path) as f:
            loaded = json.load(f)
        assert loaded["version"] == _SETTINGS_VERSION
        assert loaded["version"] == 6

    def test_save_firmware_cache_stamps_version(self, adapter):
        adapter.save_firmware_cache({"snes": {"files": []}})
        cache_path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(cache_path) as f:
            loaded = json.load(f)
        assert loaded["version"] == _FIRMWARE_CACHE_VERSION


# ── Version mismatch on load — caches discarded ──────────────────────────────


class TestVersionMismatchOnLoad:
    def test_load_firmware_cache_version_mismatch_discards(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(cache_path, "w") as f:
            json.dump({"version": 999, "snes": {"files": []}}, f)
        result = adapter.load_firmware_cache()
        assert result == {"version": _FIRMWARE_CACHE_VERSION}
        assert "snes" not in result

    def test_load_firmware_cache_no_version_discards(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(cache_path, "w") as f:
            json.dump({"snes": {"files": []}}, f)
        result = adapter.load_firmware_cache()
        assert result == {"version": _FIRMWARE_CACHE_VERSION}
        assert "snes" not in result


# ── Loading edge cases ─────────────────────────────────────────────────────────


class TestLoadingEdgeCases:
    def test_load_settings_fresh_defaults(self, adapter):
        result = adapter.load_settings()
        for key, default_value in DEFAULT_SETTINGS.items():
            assert result[key] == default_value
        # Fresh install: no file → version backfilled to 0
        assert result["version"] == 0

    def test_load_settings_backfills_version_0(self, adapter):
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"romm_url": "http://example.com"}, f)
        os.chmod(settings_path, 0o600)
        result = adapter.load_settings()
        assert result["version"] == 0
        assert result["romm_url"] == "http://example.com"

    def test_load_settings_preserves_version(self, adapter):
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"romm_url": "http://example.com", "version": 1}, f)
        os.chmod(settings_path, 0o600)
        result = adapter.load_settings()
        assert result["version"] == 1

    def test_load_settings_corrupt_json_returns_defaults(self, adapter):
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            f.write("NOT_VALID_JSON{{{")
        result = adapter.load_settings()
        for key, default_value in DEFAULT_SETTINGS.items():
            assert result[key] == default_value

    def test_load_settings_applies_defaults_for_missing_keys(self, adapter):
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"romm_url": "http://custom.com"}, f)
        os.chmod(settings_path, 0o600)
        result = adapter.load_settings()
        assert result["romm_url"] == "http://custom.com"
        assert result["steam_input_mode"] == "default"
        assert result["romm_allow_insecure_ssl"] is False

    def test_load_firmware_cache_missing_file_returns_empty(self, adapter):
        result = adapter.load_firmware_cache()
        assert result == {"version": _FIRMWARE_CACHE_VERSION}

    def test_load_firmware_cache_corrupt_json_returns_empty(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(cache_path, "w") as f:
            f.write("CORRUPT{{{")
        result = adapter.load_firmware_cache()
        assert result == {"version": _FIRMWARE_CACHE_VERSION}

    def test_load_firmware_cache_valid_version_returns_data(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(cache_path, "w") as f:
            json.dump({"version": _FIRMWARE_CACHE_VERSION, "snes": {"files": []}}, f)
        result = adapter.load_firmware_cache()
        assert result["snes"] == {"files": []}
        assert result["version"] == _FIRMWARE_CACHE_VERSION

    def test_load_firmware_cache_non_dict_json_returns_empty(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(cache_path, "w") as f:
            json.dump([1, 2, 3], f)
        result = adapter.load_firmware_cache()
        assert result == {"version": _FIRMWARE_CACHE_VERSION}

    def test_load_settings_fixes_permissions(self, adapter):
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"romm_url": "http://example.com"}, f)
        os.chmod(settings_path, 0o644)
        adapter.load_settings()
        mode = os.stat(settings_path).st_mode & 0o777
        assert mode == 0o600

    def test_save_settings_sets_permissions(self, adapter):
        adapter.save_settings({"romm_url": "http://example.com"})
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        mode = os.stat(settings_path).st_mode & 0o777
        assert mode == 0o600


# ── Save-sync state (legacy read — consumed only by the settings fold) ───────────


class TestLoadSaveSyncState:
    def _write(self, adapter, raw: str) -> None:
        path = os.path.join(adapter._runtime_dir, "save_sync_state.json")
        with open(path, "w") as f:
            f.write(raw)

    def test_load_round_trip(self, adapter):
        payload = {
            "version": 1,
            "device_id": "dev-1",
            "saves": {"42": {"files": {"game.srm": {"tracked_save_id": 7}}}},
        }
        self._write(adapter, json.dumps(payload))
        assert adapter.load_save_sync_state() == payload

    def test_load_missing_file_returns_none(self, adapter):
        assert adapter.load_save_sync_state() is None

    def test_load_corrupt_json_returns_none(self, adapter):
        self._write(adapter, "CORRUPT{{{")
        assert adapter.load_save_sync_state() is None

    def test_load_non_dict_json_returns_none(self, adapter):
        self._write(adapter, json.dumps([1, 2, 3]))
        assert adapter.load_save_sync_state() is None


class TestFirmwareCachePersisterAdapter:
    def test_save_writes_through_persistence_adapter(self, adapter):
        wrapper = FirmwareCachePersisterAdapter(adapter)
        wrapper.save({"items": [{"id": 1, "file_name": "bios.bin"}], "cached_at": 1700.0})

        path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(path) as f:
            on_disk = json.load(f)
        # PersistenceAdapter.save_firmware_cache stamps the version key
        assert on_disk["items"] == [{"id": 1, "file_name": "bios.bin"}]
        assert on_disk["cached_at"] == 1700.0
        assert on_disk["version"] == _FIRMWARE_CACHE_VERSION

    def test_save_then_load_round_trip(self, adapter):
        wrapper = FirmwareCachePersisterAdapter(adapter)
        payload = {"items": [{"id": 7, "file_name": "scph5501.bin"}], "cached_at": 42.0}
        wrapper.save(payload)

        loaded = wrapper.load()
        assert loaded["items"] == payload["items"]
        assert loaded["cached_at"] == payload["cached_at"]
        assert loaded["version"] == _FIRMWARE_CACHE_VERSION

    def test_load_returns_empty_dict_when_missing(self, adapter):
        wrapper = FirmwareCachePersisterAdapter(adapter)
        # No file written yet — should mirror PersistenceAdapter.load_firmware_cache
        # behaviour and return the version-stamped empty dict, never None.
        loaded = wrapper.load()
        assert loaded == {"version": _FIRMWARE_CACHE_VERSION}
