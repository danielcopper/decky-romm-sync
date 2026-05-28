"""Tests for domain/state_migrations.py — pure migration functions."""

from domain.state_migrations import (
    fold_legacy_save_sync_settings,
    migrate_settings,
    migrate_state,
)


class TestMigrateSettings:
    def test_migrate_settings_v0_disable_steam_input_true(self):
        data = {"version": 0, "disable_steam_input": True}
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert "disable_steam_input" not in result
        assert result["version"] == 4

    def test_migrate_settings_v0_disable_steam_input_false(self):
        data = {"version": 0, "disable_steam_input": False}
        result = migrate_settings(data)
        assert "disable_steam_input" not in result
        assert "steam_input_mode" not in result  # False → no override set
        assert result["version"] == 4

    def test_migrate_settings_v0_debug_logging_true(self):
        data = {"version": 0, "debug_logging": True}
        result = migrate_settings(data)
        assert result["log_level"] == "debug"
        assert "debug_logging" not in result
        assert result["version"] == 4

    def test_migrate_settings_v0_debug_logging_false(self):
        data = {"version": 0, "debug_logging": False}
        result = migrate_settings(data)
        assert "debug_logging" not in result
        assert "log_level" not in result  # False → no log_level override set
        assert result["version"] == 4

    def test_migrate_settings_v0_both_deprecated(self):
        data = {"version": 0, "disable_steam_input": True, "debug_logging": True}
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert result["log_level"] == "debug"
        assert "disable_steam_input" not in result
        assert "debug_logging" not in result
        assert result["version"] == 4

    def test_migrate_settings_v0_no_deprecated_keys(self):
        data = {"version": 0, "romm_url": "http://example.com"}
        result = migrate_settings(data)
        assert result["romm_url"] == "http://example.com"
        assert result["version"] == 4

    def test_migrate_settings_v3_only_bumps_version(self):
        """v3 → v4 only advances the version stamp (the cross-file save-sync
        fold is orchestrated in bootstrap, not here)."""
        data = {"version": 3, "romm_url": "http://example.com", "log_level": "warn"}
        result = migrate_settings(data)
        assert result == {"version": 4, "romm_url": "http://example.com", "log_level": "warn"}

    def test_migrate_settings_v4_no_change(self):
        data = {"version": 4, "romm_url": "http://example.com", "log_level": "warn"}
        result = migrate_settings(data)
        assert result == {"version": 4, "romm_url": "http://example.com", "log_level": "warn"}

    def test_migrate_settings_fresh_empty(self):
        data = {}
        result = migrate_settings(data)
        assert result["version"] == 4
        assert "disable_steam_input" not in result
        assert "debug_logging" not in result

    def test_migrate_settings_missing_version_treated_as_v0(self):
        data = {"romm_url": "http://example.com", "disable_steam_input": True}
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert result["version"] == 4

    def test_migrate_settings_debug_logging_true_overrides_log_level(self):
        """When debug_logging=True is being migrated, log_level is set to 'debug' unconditionally.

        This handles the case where load_settings() has already applied the 'warn'
        default before migration runs — the migration must win.
        """
        data = {"version": 0, "debug_logging": True, "log_level": "warn"}
        result = migrate_settings(data)
        assert result["log_level"] == "debug"
        assert "debug_logging" not in result
        assert result["version"] == 4

    def test_migrate_settings_idempotent(self):
        data = {"version": 0, "disable_steam_input": True, "debug_logging": True}
        result1 = migrate_settings(data.copy())
        result2 = migrate_settings(result1.copy())
        assert result1 == result2

    def test_migrate_settings_does_not_mutate_caller_dict(self):
        data = {"version": 0, "disable_steam_input": True, "debug_logging": True}
        original = dict(data)
        migrate_settings(data)
        assert data == original


class TestMigrateSettingsV3Collections:
    """v<3 → v3 migration: split flat ``enabled_collections`` into 3 buckets."""

    def test_numeric_keys_move_to_user_bucket(self):
        data = {"version": 1, "enabled_collections": {"3": True, "4": False, "42": True}}
        result = migrate_settings(data)
        assert result["enabled_collections"] == {
            "user": {"3": True, "4": False, "42": True},
            "smart": {},
            "franchise": {},
        }
        assert result["version"] == 4

    def test_base64_keys_move_to_franchise_bucket(self):
        b64 = "eyJuYW1lIjogIkFuIFRoZSBNYXJpbyJ9"
        data = {"version": 1, "enabled_collections": {b64: True}}
        result = migrate_settings(data)
        assert result["enabled_collections"]["franchise"] == {b64: True}
        assert result["enabled_collections"]["user"] == {}
        assert result["enabled_collections"]["smart"] == {}

    def test_mixed_keys_split_correctly(self):
        b64 = "eyJ4IjogMX0="
        data = {
            "version": 0,
            "enabled_collections": {"1": True, "42": False, b64: True},
        }
        result = migrate_settings(data)
        assert result["enabled_collections"] == {
            "user": {"1": True, "42": False},
            "smart": {},
            "franchise": {b64: True},
        }
        assert result["version"] == 4

    def test_smart_bucket_always_starts_empty(self):
        """Pre-v3 users had no smart collections — bucket must start empty."""
        data = {"version": 1, "enabled_collections": {"1": True}}
        result = migrate_settings(data)
        assert result["enabled_collections"]["smart"] == {}

    def test_empty_enabled_collections_yields_empty_buckets(self):
        data = {"version": 1, "enabled_collections": {}}
        result = migrate_settings(data)
        assert result["enabled_collections"] == {"user": {}, "smart": {}, "franchise": {}}

    def test_missing_enabled_collections_no_action(self):
        """When ``enabled_collections`` is absent the migration does nothing to that key."""
        data = {"version": 1, "romm_url": "x"}
        result = migrate_settings(data)
        assert "enabled_collections" not in result
        assert result["version"] == 4

    def test_already_nested_value_passes_through_unchanged(self):
        """Defensive: a half-stamped v3-shaped value must not be re-split."""
        already_nested = {
            "user": {"1": True},
            "smart": {"5": True},
            "franchise": {"abc": False},
        }
        data = {"version": 1, "enabled_collections": already_nested}
        result = migrate_settings(data)
        assert result["enabled_collections"] == already_nested
        assert result["version"] == 4

    def test_partial_nested_value_normalized_with_missing_buckets(self):
        """A partial-nested value (only one bucket present) is normalized to all three buckets."""
        data = {"version": 2, "enabled_collections": {"user": {"5": True}}}
        result = migrate_settings(data)
        assert result["enabled_collections"] == {
            "user": {"5": True},
            "smart": {},
            "franchise": {},
        }
        assert result["version"] == 4

    def test_partial_nested_two_buckets_fills_missing_third(self):
        """Partial-nested with two bucket keys — missing bucket is filled empty."""
        data = {
            "version": 2,
            "enabled_collections": {"user": {"1": True}, "franchise": {"abc": True}},
        }
        result = migrate_settings(data)
        assert result["enabled_collections"] == {
            "user": {"1": True},
            "smart": {},
            "franchise": {"abc": True},
        }
        assert result["version"] == 4

    def test_v0_to_v3_runs_both_steps(self):
        """A v0 file with both deprecated keys AND old enabled_collections gets both migrations."""
        data = {
            "version": 0,
            "disable_steam_input": True,
            "enabled_collections": {"1": True},
        }
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert "disable_steam_input" not in result
        assert result["enabled_collections"] == {
            "user": {"1": True},
            "smart": {},
            "franchise": {},
        }
        assert result["version"] == 4

    def test_negative_numeric_string_keys_go_to_user(self):
        """``key.lstrip('-').isdigit()`` accepts ``-1`` as a numeric id."""
        data = {"version": 1, "enabled_collections": {"-1": True}}
        result = migrate_settings(data)
        assert result["enabled_collections"]["user"] == {"-1": True}

    def test_v4_file_no_resplit(self):
        """A v4 file with the nested shape is unchanged."""
        data = {
            "version": 4,
            "enabled_collections": {"user": {"1": True}, "smart": {}, "franchise": {}},
        }
        result = migrate_settings(data)
        assert result == data

    def test_v3_migration_does_not_mutate_caller_dict(self):
        data = {"version": 1, "enabled_collections": {"1": True, "abc": True}}
        original = {"version": 1, "enabled_collections": {"1": True, "abc": True}}
        migrate_settings(data)
        assert data == original


class TestFoldLegacySaveSyncSettings:
    """v3 → v4 cross-file lift (#822): save-sync knobs + device_name move from
    save_sync_state.json into settings.json."""

    def _base_settings(self) -> dict:
        """A settings dict carrying the DEFAULT_SETTINGS placeholders."""
        return {
            "version": 3,
            "romm_url": "http://example.com",
            "save_sync_enabled": False,
            "sync_before_launch": True,
            "sync_after_exit": True,
            "default_slot": "default",
            "autocleanup_limit": 10,
            "device_name": None,
        }

    def test_folds_present_knobs_and_device_name(self):
        settings = self._base_settings()
        raw = {
            "device_id": "kept-id",
            "server_device_id": 7,
            "device_name": "steamdeck",
            "settings": {
                "save_sync_enabled": True,
                "sync_before_launch": False,
                "sync_after_exit": False,
                "default_slot": "alt",
                "autocleanup_limit": 3,
            },
        }
        result = fold_legacy_save_sync_settings(settings, raw)
        assert result["save_sync_enabled"] is True
        assert result["sync_before_launch"] is False
        assert result["sync_after_exit"] is False
        assert result["default_slot"] == "alt"
        assert result["autocleanup_limit"] == 3
        assert result["device_name"] == "steamdeck"
        # Unrelated settings keys are preserved.
        assert result["romm_url"] == "http://example.com"

    def test_does_not_copy_device_identity(self):
        """device_id / server_device_id stay in save_sync_state.json (#784)."""
        settings = self._base_settings()
        raw = {"device_id": "kept-id", "server_device_id": 7, "settings": {"save_sync_enabled": True}}
        result = fold_legacy_save_sync_settings(settings, raw)
        assert "device_id" not in result
        assert "server_device_id" not in result

    def test_none_raw_is_noop(self):
        settings = self._base_settings()
        result = fold_legacy_save_sync_settings(settings, None)
        assert result == settings
        assert result is not settings  # returns a copy, never the input

    def test_empty_raw_is_noop(self):
        settings = self._base_settings()
        result = fold_legacy_save_sync_settings(settings, {})
        assert result == settings

    def test_missing_settings_block_only_device_name(self):
        """No ``settings`` block → knobs untouched, device_name still folded."""
        settings = self._base_settings()
        raw = {"device_id": "id", "device_name": "deck"}
        result = fold_legacy_save_sync_settings(settings, raw)
        assert result["device_name"] == "deck"
        # Knobs keep the DEFAULT_SETTINGS placeholders.
        assert result["save_sync_enabled"] is False
        assert result["default_slot"] == "default"

    def test_missing_device_name_keeps_placeholder(self):
        settings = self._base_settings()
        raw = {"settings": {"save_sync_enabled": True}}
        result = fold_legacy_save_sync_settings(settings, raw)
        assert result["device_name"] is None

    def test_partial_settings_block_only_present_keys(self):
        """Only keys present in the legacy block overwrite; the rest keep defaults."""
        settings = self._base_settings()
        raw = {"settings": {"save_sync_enabled": True}}
        result = fold_legacy_save_sync_settings(settings, raw)
        assert result["save_sync_enabled"] is True
        assert result["sync_before_launch"] is True  # placeholder kept

    def test_never_mutates_inputs(self):
        settings = self._base_settings()
        raw = {"device_name": "deck", "settings": {"save_sync_enabled": True}}
        settings_before = dict(settings)
        raw_before = {"device_name": "deck", "settings": {"save_sync_enabled": True}}
        fold_legacy_save_sync_settings(settings, raw)
        assert settings == settings_before
        assert raw == raw_before


class TestMigrateState:
    def test_migrate_state_passthrough(self):
        data = {"version": 1, "shortcut_registry": {"1": {"app_id": 123}}}
        result = migrate_state(data)
        assert result is data  # returns same object unchanged

    def test_migrate_state_empty_dict(self):
        data = {}
        result = migrate_state(data)
        assert result == {}

    def test_migrate_state_preserves_all_keys(self):
        data = {
            "version": 1,
            "shortcut_registry": {},
            "installed_roms": {},
            "last_sync": "2024-01-01T00:00:00",
            "sync_stats": {"platforms": 3, "roms": 42},
        }
        result = migrate_state(data)
        assert result["sync_stats"]["roms"] == 42
        assert result["last_sync"] == "2024-01-01T00:00:00"
