"""Tests for the PersistenceAdapter: locking, version stamping, and load edge cases."""

import json
import logging
import os
import threading
from datetime import UTC, datetime

import pytest
from fakes.system_time import FakeClock

from adapters.persistence import (
    _SETTINGS_VERSION,
    DEFAULT_SETTINGS,
    PersistenceAdapter,
    PlatformCoreReaderAdapter,
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

    def test_settings_version_is_8(self):
        assert _SETTINGS_VERSION == 8

    def test_default_settings_carry_token_slots(self):
        assert DEFAULT_SETTINGS["romm_api_token"] is None
        assert DEFAULT_SETTINGS["romm_api_token_id"] is None
        assert DEFAULT_SETTINGS["romm_api_token_origin"] is None

    def test_default_settings_carry_empty_platform_cores(self):
        assert DEFAULT_SETTINGS["platform_cores"] == {}

    def test_load_settings_backfills_token_slots(self, adapter):
        result = adapter.load_settings()
        assert result["romm_api_token"] is None
        assert result["romm_api_token_id"] is None
        assert result["romm_api_token_origin"] is None


class TestVersionStampingOnSave:
    def test_save_settings_stamps_version(self, adapter):
        adapter.save_settings({"romm_url": "http://example.com"})
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path) as f:
            loaded = json.load(f)
        assert loaded["version"] == _SETTINGS_VERSION
        assert loaded["version"] == 8


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


# ── Crash-safe write: fsync(tmp) before rename, fsync(dir) after ─────────────────


class TestCrashSafeWrite:
    def test_fsync_called_on_file_then_dir_around_replace(self, adapter, monkeypatch):
        """fsync must hit the tmp file fd BEFORE os.replace and the directory fd
        AFTER it, so both the tmp bytes and the rename's directory entry are
        durable on power loss."""
        events: list[str] = []
        real_fsync = os.fsync
        real_replace = os.replace
        settings_path = os.path.join(adapter._settings_dir, "settings.json")

        def spy_fsync(fd):
            # A directory fd is not writable; distinguish it from the file fd by
            # checking whether the fd refers to a directory.
            kind = "dir" if os.path.isdir(f"/proc/self/fd/{fd}") else "file"
            events.append(f"fsync_{kind}")
            return real_fsync(fd)

        def spy_replace(src, dst):
            events.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", spy_fsync)
        monkeypatch.setattr(os, "replace", spy_replace)

        adapter.save_settings({"romm_url": "http://example.com"})

        assert events == ["fsync_file", "replace", "fsync_dir"], events
        # Content is actually on disk and valid.
        with open(settings_path) as f:
            assert json.load(f)["romm_url"] == "http://example.com"

    def test_dir_fsync_failure_is_swallowed(self, adapter, monkeypatch):
        """A directory fsync that raises OSError must NOT fail the write — the
        content is already durable via the tmp-file fsync + rename."""
        real_fsync = os.fsync
        settings_path = os.path.join(adapter._settings_dir, "settings.json")

        def flaky_fsync(fd):
            if os.path.isdir(f"/proc/self/fd/{fd}"):
                raise OSError("dir fsync unsupported")
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", flaky_fsync)

        # Must not raise.
        adapter.save_settings({"romm_url": "http://example.com"})
        with open(settings_path) as f:
            assert json.load(f)["romm_url"] == "http://example.com"

    def test_write_failure_cleans_up_tmp_and_raises(self, adapter, monkeypatch):
        """A failure inside the write (before rename) must unlink the tmp file
        and re-raise, leaving no half-written settings.json."""
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        tmp_path = settings_path + ".tmp"

        def boom(*_a, **_k):
            raise RuntimeError("disk full")

        monkeypatch.setattr(json, "dump", boom)

        with pytest.raises(RuntimeError, match="disk full"):
            adapter.save_settings({"romm_url": "http://example.com"})
        assert not os.path.exists(tmp_path)
        assert not os.path.exists(settings_path)


# ── Version stamping never down-stamps ───────────────────────────────────────────


class TestVersionNoDownStamp:
    def _saved(self, adapter):
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path) as f:
            return json.load(f)

    def test_newer_version_preserved(self, adapter):
        """A settings dict from a NEWER plugin (version > current) is preserved,
        never down-stamped to _SETTINGS_VERSION."""
        future = _SETTINGS_VERSION + 5
        adapter.save_settings({"romm_url": "http://example.com", "version": future})
        assert self._saved(adapter)["version"] == future

    def test_older_version_stamped_up(self, adapter):
        adapter.save_settings({"romm_url": "http://example.com", "version": 1})
        assert self._saved(adapter)["version"] == _SETTINGS_VERSION

    def test_absent_version_stamped_to_current(self, adapter):
        adapter.save_settings({"romm_url": "http://example.com"})
        assert self._saved(adapter)["version"] == _SETTINGS_VERSION

    def test_equal_version_unchanged(self, adapter):
        adapter.save_settings({"romm_url": "http://example.com", "version": _SETTINGS_VERSION})
        assert self._saved(adapter)["version"] == _SETTINGS_VERSION

    @pytest.mark.parametrize("bad_version", [None, "", "abc", "v8", "8.0", [], {}])
    def test_malformed_version_coerced(self, adapter, bad_version):
        """A non-int / non-numeric-truthy / falsy stored version must NOT raise —
        it coerces to 0, then stamps up to current. The truthy-string cases
        (``"abc"``, ``"v8"``) are the regression guard: ``int("abc")`` raises
        ValueError, which would crash the boot-time save."""
        adapter.save_settings({"romm_url": "http://example.com", "version": bad_version})
        assert self._saved(adapter)["version"] == _SETTINGS_VERSION


# ── Corrupt-file quarantine + one-shot reset notice ──────────────────────────────


class TestCorruptQuarantine:
    def _make_adapter(self, tmp_path, logger, clock):
        settings_dir = str(tmp_path / "settings")
        runtime_dir = str(tmp_path / "runtime")
        os.makedirs(settings_dir, exist_ok=True)
        os.makedirs(runtime_dir, exist_ok=True)
        return PersistenceAdapter(settings_dir=settings_dir, runtime_dir=runtime_dir, logger=logger, clock=clock)

    def test_corrupt_file_backed_up_with_clock_stamp(self, tmp_path, logger):
        clock = FakeClock(now=datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC))
        adapter = self._make_adapter(tmp_path, logger, clock)
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            f.write("TRUNCATED{{{")

        result = adapter.load_settings()

        expected_stamp = int(clock.time())
        backup_name = f"settings.json.corrupt-{expected_stamp}"
        backup_path = os.path.join(adapter._settings_dir, backup_name)
        # Original unparseable file moved aside (gone from its original name).
        assert not os.path.exists(settings_path)
        assert os.path.exists(backup_path)
        with open(backup_path) as f:
            assert f.read() == "TRUNCATED{{{"
        # Defaults returned.
        for key, default_value in DEFAULT_SETTINGS.items():
            assert result[key] == default_value
        # Transient flag set with the backup name.
        assert adapter._corrupt_reset == {"backed_up_to": backup_name}

    def test_two_corruptions_same_clock_second_keep_both_backups(self, tmp_path, logger):
        """Two corruptions at the same wall-clock second must NOT clobber each
        other — the second backup gets a ``-<n>`` suffix and the first survives
        with its distinct original contents."""
        clock = FakeClock(now=datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC))
        adapter = self._make_adapter(tmp_path, logger, clock)
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        stamp = int(clock.time())

        # First corruption.
        with open(settings_path, "w") as f:
            f.write("FIRST_CORRUPT{{{")
        adapter.load_settings()
        first_backup = os.path.join(adapter._settings_dir, f"settings.json.corrupt-{stamp}")

        # Second corruption at the SAME clock second (FakeClock not advanced).
        with open(settings_path, "w") as f:
            f.write("SECOND_CORRUPT{{{")
        adapter.load_settings()
        second_backup = os.path.join(adapter._settings_dir, f"settings.json.corrupt-{stamp}-1")

        # Both backups exist with their distinct original bytes — the first is
        # NOT overwritten by the second.
        assert os.path.exists(first_backup)
        assert os.path.exists(second_backup)
        with open(first_backup) as f:
            assert f.read() == "FIRST_CORRUPT{{{"
        with open(second_backup) as f:
            assert f.read() == "SECOND_CORRUPT{{{"
        # The notice points at the most recent backup.
        assert adapter._corrupt_reset == {"backed_up_to": f"settings.json.corrupt-{stamp}-1"}

    def test_corrupt_file_logs_error(self, tmp_path, logger, caplog):
        adapter = self._make_adapter(tmp_path, logger, FakeClock())
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            f.write("{{{not json")
        with caplog.at_level(logging.ERROR):
            adapter.load_settings()
        assert any("corrupt" in r.message.lower() for r in caplog.records)

    def test_first_run_missing_file_no_backup_no_flag(self, tmp_path, logger):
        adapter = self._make_adapter(tmp_path, logger, FakeClock())
        result = adapter.load_settings()
        # No .corrupt- file created.
        assert not any(n.startswith("settings.json.corrupt-") for n in os.listdir(adapter._settings_dir))
        assert adapter._corrupt_reset is None
        assert result["romm_url"] == ""

    def test_valid_file_no_flag(self, tmp_path, logger):
        adapter = self._make_adapter(tmp_path, logger, FakeClock())
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            json.dump({"romm_url": "http://ok.com"}, f)
        os.chmod(settings_path, 0o600)
        result = adapter.load_settings()
        assert result["romm_url"] == "http://ok.com"
        assert adapter._corrupt_reset is None
        assert not any(n.startswith("settings.json.corrupt-") for n in os.listdir(adapter._settings_dir))

    def test_failed_backup_rename_still_returns_defaults(self, tmp_path, logger, monkeypatch):
        """If the backup rename fails (e.g. perms), boot must not crash — defaults
        are returned and the flag is NOT set."""
        adapter = self._make_adapter(tmp_path, logger, FakeClock())
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            f.write("CORRUPT{{{")

        def boom(_src, _dst):
            raise OSError("permission denied")

        monkeypatch.setattr(os, "replace", boom)
        result = adapter.load_settings()
        for key, default_value in DEFAULT_SETTINGS.items():
            assert result[key] == default_value
        assert adapter._corrupt_reset is None

    def test_corrupt_perms_still_enforced_on_backup_path(self, tmp_path, logger):
        """The fresh defaults written after a reset must still land at 0600 on the
        next save — quarantine does not relax permission enforcement."""
        adapter = self._make_adapter(tmp_path, logger, FakeClock())
        settings_path = os.path.join(adapter._settings_dir, "settings.json")
        with open(settings_path, "w") as f:
            f.write("CORRUPT{{{")
        defaults = adapter.load_settings()
        adapter.save_settings(defaults)
        assert os.stat(settings_path).st_mode & 0o777 == 0o600


class TestResetNotice:
    def test_clean_boot_returns_reset_false(self, adapter):
        assert adapter.consume_settings_reset_notice() == {"reset": False, "backed_up_to": None}

    def test_after_corruption_returns_reset_true_then_clears(self, tmp_path, logger):
        clock = FakeClock(now=datetime(2026, 6, 13, 12, 0, 0, tzinfo=UTC))
        settings_dir = str(tmp_path / "settings")
        runtime_dir = str(tmp_path / "runtime")
        os.makedirs(settings_dir, exist_ok=True)
        os.makedirs(runtime_dir, exist_ok=True)
        adapter = PersistenceAdapter(settings_dir=settings_dir, runtime_dir=runtime_dir, logger=logger, clock=clock)
        with open(os.path.join(settings_dir, "settings.json"), "w") as f:
            f.write("CORRUPT{{{")
        adapter.load_settings()

        first = adapter.consume_settings_reset_notice()
        assert first["reset"] is True
        assert first["backed_up_to"] == f"settings.json.corrupt-{int(clock.time())}"
        # Drains once: a second read is clean.
        assert adapter.consume_settings_reset_notice() == {"reset": False, "backed_up_to": None}


class TestClockInjection:
    def test_default_clock_used_when_not_injected(self, tmp_path, logger):
        """Constructing without an explicit clock must not crash and the corrupt
        backup name carries an integer stamp from the real SystemClock."""
        settings_dir = str(tmp_path / "settings")
        runtime_dir = str(tmp_path / "runtime")
        os.makedirs(settings_dir, exist_ok=True)
        os.makedirs(runtime_dir, exist_ok=True)
        adapter = PersistenceAdapter(settings_dir=settings_dir, runtime_dir=runtime_dir, logger=logger)
        with open(os.path.join(settings_dir, "settings.json"), "w") as f:
            f.write("CORRUPT{{{")
        adapter.load_settings()
        backups = [n for n in os.listdir(settings_dir) if n.startswith("settings.json.corrupt-")]
        assert len(backups) == 1
        stamp = backups[0].rsplit("-", 1)[1]
        assert stamp.isdigit()


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


# ── PlatformCoreReaderAdapter (per-platform core read over live settings) ────────


class TestPlatformCoreReaderAdapter:
    def test_returns_label_for_configured_platform(self):
        reader = PlatformCoreReaderAdapter({"platform_cores": {"snes": "bsnes", "gba": "mGBA"}})
        assert reader.get_platform_core("snes") == "bsnes"
        assert reader.get_platform_core("gba") == "mGBA"

    def test_returns_none_for_absent_platform(self):
        reader = PlatformCoreReaderAdapter({"platform_cores": {"snes": "bsnes"}})
        assert reader.get_platform_core("psx") is None

    def test_returns_none_when_map_empty(self):
        reader = PlatformCoreReaderAdapter({"platform_cores": {}})
        assert reader.get_platform_core("snes") is None

    def test_returns_none_when_key_missing(self):
        # Defensive: a settings dict without platform_cores at all.
        reader = PlatformCoreReaderAdapter({})
        assert reader.get_platform_core("snes") is None

    def test_reads_live_dict_not_a_snapshot(self):
        """The adapter binds the live dict — a later write is visible on the next read."""
        settings: dict[str, object] = {"platform_cores": {}}
        reader = PlatformCoreReaderAdapter(settings)
        assert reader.get_platform_core("snes") is None
        # Mutate the same dict the adapter holds.
        settings["platform_cores"]["snes"] = "bsnes"  # type: ignore[index]
        assert reader.get_platform_core("snes") == "bsnes"
