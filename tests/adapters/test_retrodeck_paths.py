"""Tests for adapters.retrodeck_paths.RetroDeckPathsAdapter."""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any
from unittest.mock import patch

from adapters.retrodeck_paths import RetroDeckPathsAdapter


def _make_adapter(tmp_path, config: dict[str, Any] | None = None) -> RetroDeckPathsAdapter:
    """Create adapter with optional retrodeck.json config."""
    user_home = str(tmp_path)
    if config is not None:
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "retrodeck.json").write_text(json.dumps(config))
    return RetroDeckPathsAdapter(user_home=user_home, logger=logging.getLogger("test"))


class TestPathResolution:
    def test_bios_path_from_config(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"paths": {"bios_path": "/custom/bios"}})
        assert adapter.bios_path() == "/custom/bios"

    def test_bios_path_fallback(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter.bios_path() == os.path.join(str(tmp_path), "retrodeck", "bios")

    def test_roms_path_from_config(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"paths": {"roms_path": "/custom/roms"}})
        assert adapter.roms_path() == "/custom/roms"

    def test_roms_path_fallback(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter.roms_path() == os.path.join(str(tmp_path), "retrodeck", "roms")

    def test_saves_path_from_config(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"paths": {"saves_path": "/custom/saves"}})
        assert adapter.saves_path() == "/custom/saves"

    def test_saves_path_fallback(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter.saves_path() == os.path.join(str(tmp_path), "retrodeck", "saves")

    def test_retrodeck_home_from_config(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"paths": {"rd_home_path": "/custom/home"}})
        assert adapter.retrodeck_home() == "/custom/home"

    def test_retrodeck_home_fallback(self, tmp_path):
        adapter = _make_adapter(tmp_path)
        assert adapter.retrodeck_home() == os.path.join(str(tmp_path), "retrodeck", "")

    def test_empty_path_uses_fallback(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"paths": {"roms_path": ""}})
        assert adapter.roms_path() == os.path.join(str(tmp_path), "retrodeck", "roms")

    def test_missing_paths_key_uses_fallback(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"other": "data"})
        assert adapter.roms_path() == os.path.join(str(tmp_path), "retrodeck", "roms")

    def test_malformed_json_uses_fallback(self, tmp_path):
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "retrodeck.json").write_text("not valid json")
        adapter = RetroDeckPathsAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        assert adapter.bios_path() == os.path.join(str(tmp_path), "retrodeck", "bios")


class TestTTLCache:
    def test_cache_returns_same_value(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"paths": {"bios_path": "/first"}})
        assert adapter.bios_path() == "/first"
        # Overwrite config — should still return cached value
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        (config_dir / "retrodeck.json").write_text(json.dumps({"paths": {"bios_path": "/second"}}))
        assert adapter.bios_path() == "/first"

    def test_cache_expires_after_ttl(self, tmp_path):
        adapter = _make_adapter(tmp_path, {"paths": {"bios_path": "/first"}})
        assert adapter.bios_path() == "/first"
        # Force cache expiry
        adapter._cache_time = time.monotonic() - 31
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        (config_dir / "retrodeck.json").write_text(json.dumps({"paths": {"bios_path": "/second"}}))
        assert adapter.bios_path() == "/second"

    def test_failed_load_is_retried(self, tmp_path):
        """A failed load is not cached — later successful loads are picked up immediately.

        The TTL cache only stores positive results. When ``_load_config``
        returns None the cache stays empty, so the next call re-reads
        the file. This lets the adapter recover automatically when a
        missing ``retrodeck.json`` is created at runtime, without
        waiting for the 30-second TTL.
        """
        adapter = _make_adapter(tmp_path)  # no config — returns fallback
        fallback = os.path.join(str(tmp_path), "retrodeck", "bios")
        assert adapter.bios_path() == fallback

        # Drop a valid config file
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "retrodeck.json").write_text(json.dumps({"paths": {"bios_path": "/picked/up"}}))

        # Picked up on the next call — no need to wait out the TTL.
        assert adapter.bios_path() == "/picked/up"


class TestLoadConfigLogging:
    def test_load_config_logs_warning_on_json_error(self, tmp_path, caplog):
        """Invalid JSON triggers a warning log and falls back to the
        defaults — the failure is no longer silently swallowed."""
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "retrodeck.json").write_text("not valid json")
        adapter = RetroDeckPathsAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))

        with caplog.at_level(logging.WARNING):
            result = adapter.bios_path()

        assert result == os.path.join(str(tmp_path), "retrodeck", "bios")
        assert any("Failed to load RetroDECK config" in rec.message for rec in caplog.records)

    def test_load_config_logs_warning_on_permission_error(self, tmp_path, caplog):
        """PermissionError on the config file triggers a warning log and
        falls back to the defaults."""
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_file = config_dir / "retrodeck.json"
        config_file.write_text(json.dumps({"paths": {"bios_path": "/should/not/be/read"}}))

        real_open = open

        def fake_open(path, *args, **kwargs):
            if str(path) == str(config_file):
                raise PermissionError(f"denied: {path}")
            return real_open(path, *args, **kwargs)

        adapter = RetroDeckPathsAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))
        with patch("builtins.open", side_effect=fake_open), caplog.at_level(logging.WARNING):
            result = adapter.bios_path()

        assert result == os.path.join(str(tmp_path), "retrodeck", "bios")
        assert any("Failed to load RetroDECK config" in rec.message for rec in caplog.records)

    def test_load_config_does_not_log_on_missing_file(self, tmp_path, caplog):
        """A missing ``retrodeck.json`` is the expected fresh-install
        fallback path and must NOT spam the log on every read."""
        adapter = RetroDeckPathsAdapter(user_home=str(tmp_path), logger=logging.getLogger("test"))

        with caplog.at_level(logging.WARNING):
            result = adapter.bios_path()

        assert result == os.path.join(str(tmp_path), "retrodeck", "bios")
        assert not any("Failed to load RetroDECK config" in rec.message for rec in caplog.records)
