"""Tests for domain/state_migrations.py — pure migration functions."""

from domain.state_migrations import migrate_settings, migrate_state


class TestMigrateSettings:
    def test_migrate_settings_v0_disable_steam_input_true(self):
        data = {"version": 0, "disable_steam_input": True}
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert "disable_steam_input" not in result
        assert result["version"] == 3

    def test_migrate_settings_v0_disable_steam_input_false(self):
        data = {"version": 0, "disable_steam_input": False}
        result = migrate_settings(data)
        assert "disable_steam_input" not in result
        assert "steam_input_mode" not in result  # False → no override set
        assert result["version"] == 3

    def test_migrate_settings_v0_debug_logging_true(self):
        data = {"version": 0, "debug_logging": True}
        result = migrate_settings(data)
        assert result["log_level"] == "debug"
        assert "debug_logging" not in result
        assert result["version"] == 3

    def test_migrate_settings_v0_debug_logging_false(self):
        data = {"version": 0, "debug_logging": False}
        result = migrate_settings(data)
        assert "debug_logging" not in result
        assert "log_level" not in result  # False → no log_level override set
        assert result["version"] == 3

    def test_migrate_settings_v0_both_deprecated(self):
        data = {"version": 0, "disable_steam_input": True, "debug_logging": True}
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert result["log_level"] == "debug"
        assert "disable_steam_input" not in result
        assert "debug_logging" not in result
        assert result["version"] == 3

    def test_migrate_settings_v0_no_deprecated_keys(self):
        data = {"version": 0, "romm_url": "http://example.com"}
        result = migrate_settings(data)
        assert result["romm_url"] == "http://example.com"
        assert result["version"] == 3

    def test_migrate_settings_v3_no_change(self):
        data = {"version": 3, "romm_url": "http://example.com", "log_level": "warn"}
        result = migrate_settings(data)
        assert result == {"version": 3, "romm_url": "http://example.com", "log_level": "warn"}

    def test_migrate_settings_fresh_empty(self):
        data = {}
        result = migrate_settings(data)
        assert result["version"] == 3
        assert "disable_steam_input" not in result
        assert "debug_logging" not in result

    def test_migrate_settings_missing_version_treated_as_v0(self):
        data = {"romm_url": "http://example.com", "disable_steam_input": True}
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert result["version"] == 3

    def test_migrate_settings_debug_logging_true_overrides_log_level(self):
        """When debug_logging=True is being migrated, log_level is set to 'debug' unconditionally.

        This handles the case where load_settings() has already applied the 'warn'
        default before migration runs — the migration must win.
        """
        data = {"version": 0, "debug_logging": True, "log_level": "warn"}
        result = migrate_settings(data)
        assert result["log_level"] == "debug"
        assert "debug_logging" not in result
        assert result["version"] == 3

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
        assert result["version"] == 3

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
        assert result["version"] == 3

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
        assert result["version"] == 3

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
        assert result["version"] == 3

    def test_partial_nested_value_normalized_with_missing_buckets(self):
        """A partial-nested value (only one bucket present) is normalized to all three buckets."""
        data = {"version": 2, "enabled_collections": {"user": {"5": True}}}
        result = migrate_settings(data)
        assert result["enabled_collections"] == {
            "user": {"5": True},
            "smart": {},
            "franchise": {},
        }
        assert result["version"] == 3

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
        assert result["version"] == 3

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
        assert result["version"] == 3

    def test_negative_numeric_string_keys_go_to_user(self):
        """``key.lstrip('-').isdigit()`` accepts ``-1`` as a numeric id."""
        data = {"version": 1, "enabled_collections": {"-1": True}}
        result = migrate_settings(data)
        assert result["enabled_collections"]["user"] == {"-1": True}

    def test_v3_file_no_resplit(self):
        """A v3 file with the nested shape is unchanged."""
        data = {
            "version": 3,
            "enabled_collections": {"user": {"1": True}, "smart": {}, "franchise": {}},
        }
        result = migrate_settings(data)
        assert result == data

    def test_v3_migration_does_not_mutate_caller_dict(self):
        data = {"version": 1, "enabled_collections": {"1": True, "abc": True}}
        original = {"version": 1, "enabled_collections": {"1": True, "abc": True}}
        migrate_settings(data)
        assert data == original


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
