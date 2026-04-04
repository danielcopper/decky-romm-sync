"""Tests for the PersistenceAdapter: locking, version stamping, and load edge cases."""

import json
import logging
import os
import threading

import pytest

from adapters.persistence import (
    _FIRMWARE_CACHE_VERSION,
    _METADATA_CACHE_VERSION,
    _SETTINGS_VERSION,
    _STATE_VERSION,
    DEFAULT_SETTINGS,
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

    def test_save_state_creates_lock_file(self, adapter):
        adapter.save_state({"shortcut_registry": {}})
        lock_path = os.path.join(adapter._runtime_dir, "state.json.lock")
        assert os.path.exists(lock_path)

    def test_save_metadata_cache_creates_lock_file(self, adapter):
        adapter.save_metadata_cache({"1": {"title": "Game"}})
        lock_path = os.path.join(adapter._runtime_dir, "metadata_cache.json.lock")
        assert os.path.exists(lock_path)

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


class TestVersionStampingOnSave:
    def test_save_settings_stamps_version(self, adapter):
        adapter.save_settings({"romm_url": "http://example.com"})
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path) as f:
            loaded = json.load(f)
        assert loaded["version"] == _SETTINGS_VERSION

    def test_save_state_stamps_version(self, adapter):
        adapter.save_state({"shortcut_registry": {}})
        state_path = os.path.join(adapter._runtime_dir, "state.json")
        with open(state_path) as f:
            loaded = json.load(f)
        assert loaded["version"] == _STATE_VERSION

    def test_save_metadata_cache_stamps_version(self, adapter):
        adapter.save_metadata_cache({"1": {"title": "Game"}})
        cache_path = os.path.join(adapter._runtime_dir, "metadata_cache.json")
        with open(cache_path) as f:
            loaded = json.load(f)
        assert loaded["version"] == _METADATA_CACHE_VERSION

    def test_save_firmware_cache_stamps_version(self, adapter):
        adapter.save_firmware_cache({"snes": {"files": []}})
        cache_path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(cache_path) as f:
            loaded = json.load(f)
        assert loaded["version"] == _FIRMWARE_CACHE_VERSION


# ── Version mismatch on load — caches discarded ──────────────────────────────


class TestVersionMismatchOnLoad:
    def test_load_metadata_cache_version_mismatch_discards(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "metadata_cache.json")
        with open(cache_path, "w") as f:
            json.dump({"version": 999, "1": {"title": "stale"}}, f)
        result = adapter.load_metadata_cache()
        assert result == {"version": _METADATA_CACHE_VERSION}
        assert "1" not in result

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

    def test_load_metadata_cache_no_version_discards(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "metadata_cache.json")
        with open(cache_path, "w") as f:
            json.dump({"1": {"title": "stale"}}, f)
        result = adapter.load_metadata_cache()
        assert result == {"version": _METADATA_CACHE_VERSION}
        assert "1" not in result


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

    def test_load_state_merges_defaults(self, adapter):
        defaults = {"shortcut_registry": {}, "installed_roms": {}, "last_sync": None}
        state_path = os.path.join(adapter._runtime_dir, "state.json")
        with open(state_path, "w") as f:
            json.dump({"shortcut_registry": {"1": {"app_id": 123}}, "version": 1}, f)
        result = adapter.load_state(defaults)
        assert result["shortcut_registry"] == {"1": {"app_id": 123}}
        assert result["installed_roms"] == {}
        assert result["last_sync"] is None

    def test_load_state_backfills_version(self, adapter):
        defaults = {"shortcut_registry": {}}
        state_path = os.path.join(adapter._runtime_dir, "state.json")
        with open(state_path, "w") as f:
            json.dump({"shortcut_registry": {}}, f)
        result = adapter.load_state(defaults)
        assert result["version"] == _STATE_VERSION

    def test_load_state_missing_file_returns_defaults(self, adapter):
        defaults = {"shortcut_registry": {}, "installed_roms": {}}
        result = adapter.load_state(defaults)
        assert result["shortcut_registry"] == {}
        assert result["installed_roms"] == {}
        assert result["version"] == _STATE_VERSION

    def test_load_state_corrupt_json_returns_defaults(self, adapter):
        defaults = {"shortcut_registry": {}}
        state_path = os.path.join(adapter._runtime_dir, "state.json")
        with open(state_path, "w") as f:
            f.write("CORRUPT{{{")
        result = adapter.load_state(defaults)
        assert result["shortcut_registry"] == {}

    def test_load_metadata_cache_missing_file_returns_empty(self, adapter):
        result = adapter.load_metadata_cache()
        assert result == {"version": _METADATA_CACHE_VERSION}

    def test_load_firmware_cache_missing_file_returns_empty(self, adapter):
        result = adapter.load_firmware_cache()
        assert result == {"version": _FIRMWARE_CACHE_VERSION}

    def test_load_metadata_cache_corrupt_json_returns_empty(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "metadata_cache.json")
        with open(cache_path, "w") as f:
            f.write("CORRUPT{{{")
        result = adapter.load_metadata_cache()
        assert result == {"version": _METADATA_CACHE_VERSION}

    def test_load_firmware_cache_corrupt_json_returns_empty(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(cache_path, "w") as f:
            f.write("CORRUPT{{{")
        result = adapter.load_firmware_cache()
        assert result == {"version": _FIRMWARE_CACHE_VERSION}

    def test_load_metadata_cache_valid_version_returns_data(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "metadata_cache.json")
        with open(cache_path, "w") as f:
            json.dump({"version": _METADATA_CACHE_VERSION, "42": {"title": "Game"}}, f)
        result = adapter.load_metadata_cache()
        assert result["42"] == {"title": "Game"}
        assert result["version"] == _METADATA_CACHE_VERSION

    def test_load_firmware_cache_valid_version_returns_data(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "firmware_cache.json")
        with open(cache_path, "w") as f:
            json.dump({"version": _FIRMWARE_CACHE_VERSION, "snes": {"files": []}}, f)
        result = adapter.load_firmware_cache()
        assert result["snes"] == {"files": []}
        assert result["version"] == _FIRMWARE_CACHE_VERSION

    def test_load_state_non_dict_json_returns_defaults(self, adapter):
        defaults = {"shortcut_registry": {}}
        state_path = os.path.join(adapter._runtime_dir, "state.json")
        with open(state_path, "w") as f:
            json.dump([1, 2, 3], f)
        result = adapter.load_state(defaults)
        assert result["shortcut_registry"] == {}
        assert result["version"] == _STATE_VERSION

    def test_load_metadata_cache_non_dict_json_returns_empty(self, adapter):
        cache_path = os.path.join(adapter._runtime_dir, "metadata_cache.json")
        with open(cache_path, "w") as f:
            json.dump([1, 2, 3], f)
        result = adapter.load_metadata_cache()
        assert result == {"version": _METADATA_CACHE_VERSION}

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


# ── State backup (.prev) ──────────────────────────────────────────────────────


class TestStateBackup:
    def test_save_state_creates_prev_backup(self, adapter):
        """After two saves, state.json.prev should match the first save."""
        adapter.save_state({"shortcut_registry": {"1": {"app_id": 100}}})
        adapter.save_state({"shortcut_registry": {"1": {"app_id": 100}, "2": {"app_id": 200}}})
        prev_path = os.path.join(adapter._runtime_dir, "state.json.prev")
        assert os.path.exists(prev_path)
        with open(prev_path) as f:
            prev = json.load(f)
        assert "1" in prev["shortcut_registry"]
        assert "2" not in prev["shortcut_registry"]

    def test_save_state_first_save_no_prev(self, adapter):
        """First save should not create a .prev (nothing to back up)."""
        adapter.save_state({"shortcut_registry": {}})
        prev_path = os.path.join(adapter._runtime_dir, "state.json.prev")
        assert not os.path.exists(prev_path)

    def test_load_state_warns_on_empty_registry_with_prev(self, adapter, caplog):
        """Loading empty state when .prev has entries should log a warning."""
        state_path = os.path.join(adapter._runtime_dir, "state.json")
        prev_path = state_path + ".prev"
        with open(state_path, "w") as f:
            json.dump({"shortcut_registry": {}, "version": 1}, f)
        with open(prev_path, "w") as f:
            json.dump({"shortcut_registry": {"1": {"app_id": 100}}, "version": 1}, f)
        with caplog.at_level(logging.WARNING):
            adapter.load_state({"shortcut_registry": {}})
        assert "possible data loss" in caplog.text.lower()
        assert "1 entries" in caplog.text

    def test_load_state_no_warning_when_prev_also_empty(self, adapter, caplog):
        """No warning if both state and .prev have empty registries."""
        state_path = os.path.join(adapter._runtime_dir, "state.json")
        prev_path = state_path + ".prev"
        with open(state_path, "w") as f:
            json.dump({"shortcut_registry": {}, "version": 1}, f)
        with open(prev_path, "w") as f:
            json.dump({"shortcut_registry": {}, "version": 1}, f)
        with caplog.at_level(logging.WARNING):
            adapter.load_state({"shortcut_registry": {}})
        assert "possible data loss" not in caplog.text.lower()

    def test_load_state_no_warning_when_registry_has_entries(self, adapter, caplog):
        """No warning if current state has a non-empty registry."""
        state_path = os.path.join(adapter._runtime_dir, "state.json")
        with open(state_path, "w") as f:
            json.dump({"shortcut_registry": {"1": {"app_id": 100}}, "version": 1}, f)
        with caplog.at_level(logging.WARNING):
            adapter.load_state({"shortcut_registry": {}})
        assert "possible data loss" not in caplog.text.lower()

    def test_load_state_no_warning_when_no_prev(self, adapter, caplog):
        """No warning if .prev doesn't exist (fresh install)."""
        state_path = os.path.join(adapter._runtime_dir, "state.json")
        with open(state_path, "w") as f:
            json.dump({"shortcut_registry": {}, "version": 1}, f)
        with caplog.at_level(logging.WARNING):
            adapter.load_state({"shortcut_registry": {}})
        assert "possible data loss" not in caplog.text.lower()
