"""Unit tests for the ``SyncSettings`` aggregate."""

from __future__ import annotations

import pytest

from domain.sync_settings import SyncSettings


class TestDefaults:
    def test_default_construction(self):
        settings = SyncSettings()
        assert settings.save_sync_enabled is False
        assert settings.sync_before_launch is True
        assert settings.sync_after_exit is True
        assert settings.default_slot == "default"
        assert settings.autocleanup_limit == 10


class TestSaveSyncToggle:
    def test_enable_save_sync(self):
        settings = SyncSettings()
        settings.enable_save_sync()
        assert settings.save_sync_enabled is True

    def test_disable_save_sync(self):
        settings = SyncSettings(save_sync_enabled=True)
        settings.disable_save_sync()
        assert settings.save_sync_enabled is False


class TestSyncTimingFlags:
    def test_set_sync_before_launch_false(self):
        settings = SyncSettings()
        settings.set_sync_before_launch(False)
        assert settings.sync_before_launch is False

    def test_set_sync_before_launch_true(self):
        settings = SyncSettings(sync_before_launch=False)
        settings.set_sync_before_launch(True)
        assert settings.sync_before_launch is True

    def test_set_sync_after_exit_false(self):
        settings = SyncSettings()
        settings.set_sync_after_exit(False)
        assert settings.sync_after_exit is False

    def test_set_sync_after_exit_true(self):
        settings = SyncSettings(sync_after_exit=False)
        settings.set_sync_after_exit(True)
        assert settings.sync_after_exit is True


class TestDefaultSlot:
    def test_set_named_slot(self):
        settings = SyncSettings()
        settings.set_default_slot("slotA")
        assert settings.default_slot == "slotA"

    def test_set_empty_slot_becomes_none(self):
        settings = SyncSettings()
        settings.set_default_slot("")
        assert settings.default_slot is None

    def test_set_none_slot_stays_none(self):
        settings = SyncSettings()
        settings.set_default_slot(None)
        assert settings.default_slot is None


class TestAutocleanupLimit:
    def test_set_zero(self):
        settings = SyncSettings()
        settings.set_autocleanup_limit(0)
        assert settings.autocleanup_limit == 0

    def test_set_positive(self):
        settings = SyncSettings()
        settings.set_autocleanup_limit(25)
        assert settings.autocleanup_limit == 25

    def test_set_negative_raises(self):
        settings = SyncSettings()
        with pytest.raises(ValueError, match="autocleanup_limit must be >= 0"):
            settings.set_autocleanup_limit(-1)
