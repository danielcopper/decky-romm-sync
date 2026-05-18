"""Tests for domain/state_migrations.py — pure migration functions."""

from domain.state_migrations import migrate_settings, migrate_state


class TestMigrateSettings:
    def test_migrate_settings_v0_disable_steam_input_true(self):
        data = {"version": 0, "disable_steam_input": True}
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert "disable_steam_input" not in result
        assert result["version"] == 1

    def test_migrate_settings_v0_disable_steam_input_false(self):
        data = {"version": 0, "disable_steam_input": False}
        result = migrate_settings(data)
        assert "disable_steam_input" not in result
        assert "steam_input_mode" not in result  # False → no override set
        assert result["version"] == 1

    def test_migrate_settings_v0_debug_logging_true(self):
        data = {"version": 0, "debug_logging": True}
        result = migrate_settings(data)
        assert result["log_level"] == "debug"
        assert "debug_logging" not in result
        assert result["version"] == 1

    def test_migrate_settings_v0_debug_logging_false(self):
        data = {"version": 0, "debug_logging": False}
        result = migrate_settings(data)
        assert "debug_logging" not in result
        assert "log_level" not in result  # False → no log_level override set
        assert result["version"] == 1

    def test_migrate_settings_v0_both_deprecated(self):
        data = {"version": 0, "disable_steam_input": True, "debug_logging": True}
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert result["log_level"] == "debug"
        assert "disable_steam_input" not in result
        assert "debug_logging" not in result
        assert result["version"] == 1

    def test_migrate_settings_v0_no_deprecated_keys(self):
        data = {"version": 0, "romm_url": "http://example.com"}
        result = migrate_settings(data)
        assert result["romm_url"] == "http://example.com"
        assert result["version"] == 1

    def test_migrate_settings_v1_no_change(self):
        data = {"version": 1, "romm_url": "http://example.com", "log_level": "warn"}
        result = migrate_settings(data)
        assert result == {"version": 1, "romm_url": "http://example.com", "log_level": "warn"}

    def test_migrate_settings_fresh_empty(self):
        data = {}
        result = migrate_settings(data)
        assert result["version"] == 1
        assert "disable_steam_input" not in result
        assert "debug_logging" not in result

    def test_migrate_settings_missing_version_treated_as_v0(self):
        data = {"romm_url": "http://example.com", "disable_steam_input": True}
        result = migrate_settings(data)
        assert result["steam_input_mode"] == "force_off"
        assert result["version"] == 1

    def test_migrate_settings_debug_logging_true_overrides_log_level(self):
        """When debug_logging=True is being migrated, log_level is set to 'debug' unconditionally.

        This handles the case where load_settings() has already applied the 'warn'
        default before migration runs — the migration must win.
        """
        data = {"version": 0, "debug_logging": True, "log_level": "warn"}
        result = migrate_settings(data)
        assert result["log_level"] == "debug"
        assert "debug_logging" not in result
        assert result["version"] == 1

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
