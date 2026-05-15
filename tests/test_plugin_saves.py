import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# conftest.py patches decky before this import; use _make_testable_plugin for test-only attrs
from conftest import _make_retry, _make_testable_plugin
from fakes.fake_save_api import FakeSaveApi
from fakes.system_time import FakeClock, FakeSleeper, FakeUuidGen

from adapters.migration_file import MigrationFileAdapter
from adapters.persistence import PersistenceAdapter, SaveSyncStatePersisterAdapter
from adapters.romm.http import RommHttpAdapter
from adapters.save_file import SaveFileAdapter
from adapters.steam_config import SteamConfigAdapter
from domain.save_state import FileSyncState, PlaytimeEntry, RomSaveState
from services.library import LibraryService, LibraryServiceConfig
from services.migration import MigrationService, MigrationServiceConfig
from services.playtime import PlaytimeService
from services.saves import SaveService, SaveServiceConfig


@pytest.fixture
def plugin(tmp_path):
    p = _make_testable_plugin()
    p.settings = {
        "romm_url": "http://romm.local",
        "romm_user": "user",
        "romm_pass": "pass",
        "enabled_platforms": {},
        "log_level": "warn",
    }
    p._http_adapter = RommHttpAdapter(p.settings, __import__("decky").DECKY_PLUGIN_DIR, logging.getLogger("test"))
    p._romm_api = MagicMock()
    p._state = {
        "shortcut_registry": {},
        "installed_roms": {},
        "last_sync": None,
        "sync_stats": {},
    }
    p._metadata_cache = {}

    import decky

    steam_config = SteamConfigAdapter(user_home=decky.DECKY_USER_HOME, logger=decky.logger)
    p._steam_config = steam_config

    p._sync_service = LibraryService(
        romm_api=p._romm_api,
        steam_config=steam_config,
        state=p._state,
        settings=p.settings,
        metadata_cache=p._metadata_cache,
        config=LibraryServiceConfig(
            loop=asyncio.get_event_loop(),
            logger=decky.logger,
            plugin_dir=decky.DECKY_PLUGIN_DIR,
            emit=decky.emit,
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            uuid_gen=FakeUuidGen(),
            sleeper=FakeSleeper(),
            save_state=p._save_state,
            save_settings_to_disk=p._save_settings_to_disk,
            log_debug=p._log_debug,
        ),
    )
    decky.DECKY_USER_HOME = str(tmp_path)

    # Wire services with FakeSaveApi sharing the SaveFileAdapter so download
    # bytes land on the same filesystem view the service inspects.
    save_file_adapter = SaveFileAdapter()
    fake_api = FakeSaveApi(save_file=save_file_adapter)
    p._save_sync_state = SaveService.make_default_state()
    saves_path = str(tmp_path / "retrodeck" / "saves")

    p._save_sync_service = SaveService(
        romm_api=fake_api,
        retry=_make_retry(),
        settings={"log_level": "debug"},
        state=p._state,
        save_sync_state=p._save_sync_state,
        config=SaveServiceConfig(
            loop=asyncio.get_event_loop(),
            logger=logging.getLogger("test"),
            clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
            runtime_dir=str(tmp_path),
            save_sync_state_persister=SaveSyncStatePersisterAdapter(
                PersistenceAdapter(
                    settings_dir=str(tmp_path),
                    runtime_dir=str(tmp_path),
                    logger=logging.getLogger("test"),
                )
            ),
            save_file=save_file_adapter,
            get_saves_path=lambda: saves_path,
            get_roms_path=lambda: str(tmp_path / "retrodeck" / "roms"),
            get_active_core=lambda system_name, rom_filename=None: (None, None),
        ),
    )
    p._save_sync_service.init_state()

    p._playtime_service = PlaytimeService(
        romm_api=fake_api,
        retry=_make_retry(),
        save_sync_state=p._save_sync_state,
        loop=asyncio.get_event_loop(),
        logger=logging.getLogger("test"),
        clock=FakeClock(now=datetime(2026, 1, 1, tzinfo=UTC)),
        save_state=p._save_sync_service.save_state,
    )

    # Store fake_api on plugin for test access
    p._fake_api = fake_api

    # Default migration service mock — no migration pending. Tests that
    # exercise the @migration_blocked gate override this.
    p._migration_service = MagicMock()
    p._migration_service.is_retrodeck_migration_pending.return_value = False

    # Enable save sync for tests — matches pre-feature-flag behavior
    p._save_sync_state.settings.save_sync_enabled = True
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin and service loops match the running event loop for async tests."""
    loop = asyncio.get_event_loop()
    plugin.loop = loop
    plugin._save_sync_service._loop = loop
    plugin._playtime_service._loop = loop


def _install_rom(plugin, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba"):
    """Helper: register a ROM in installed_roms state."""
    plugin._state["installed_roms"][str(rom_id)] = {
        "rom_id": rom_id,
        "file_name": file_name,
        "file_path": str(tmp_path / "retrodeck" / "roms" / system / file_name),
        "system": system,
        "platform_slug": system,
        "installed_at": "2026-01-01T00:00:00",
    }


def _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"\x00" * 1024, ext=".srm"):
    """Helper: create a save file on disk."""
    saves_dir = tmp_path / "retrodeck" / "saves" / system
    saves_dir.mkdir(parents=True, exist_ok=True)
    save_file = saves_dir / (rom_name + ext)
    save_file.write_bytes(content)
    return save_file


def _server_save(
    save_id=100, rom_id=42, filename="pokemon.srm", updated_at="2026-02-17T06:00:00Z", file_size_bytes=1024
):
    """Helper: build a server save response dict (matches RomM SaveSchema)."""
    return {
        "id": save_id,
        "rom_id": rom_id,
        "file_name": filename,
        "updated_at": updated_at,
        "file_size_bytes": file_size_bytes,
        "emulator": "retroarch",
        "download_path": f"/saves/{filename}",
    }


# ============================================================================
# Device Registration (Plugin callable integration)
# ============================================================================


class TestDeviceRegistration:
    """Tests for ensure_device_registered (server registration)."""

    @pytest.mark.asyncio
    async def test_registers_with_server(self, plugin, tmp_path):
        """First call registers with server and stores device_id."""
        result = await plugin.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"]
        assert result.get("server_device_id") is not None
        assert plugin._save_sync_state.device_id == result["device_id"]

        # Persisted to disk
        path = tmp_path / "save_sync_state.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["device_id"] == result["device_id"]

    @pytest.mark.asyncio
    async def test_already_registered_returns_cached(self, plugin):
        """If device_id and server_device_id already set, returns immediately."""
        plugin._save_sync_state.device_id = "existing-uuid"
        plugin._save_sync_state.device_name = "myhost"
        plugin._save_sync_state.server_device_id = "server-uuid"

        result = await plugin.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "existing-uuid"
        assert result["device_name"] == "myhost"
        assert result["server_device_id"] == "server-uuid"

    @pytest.mark.asyncio
    async def test_sets_hostname_as_device_name(self, plugin):
        """Device name is set to the local hostname."""
        with patch("socket.gethostname", return_value="steamdeck"):
            result = await plugin.ensure_device_registered()

        assert result["device_name"] == "steamdeck"
        assert plugin._save_sync_state.device_name == "steamdeck"

    @pytest.mark.asyncio
    async def test_generates_unique_ids(self, plugin):
        """Each new registration generates a unique device ID."""
        result1 = await plugin.ensure_device_registered()
        id1 = result1["device_id"]

        # Reset state to force new registration
        plugin._save_sync_state.device_id = None
        plugin._save_sync_state.server_device_id = None
        result2 = await plugin.ensure_device_registered()
        id2 = result2["device_id"]

        assert id1 != id2


# ============================================================================
# List Devices (Plugin callable integration)
# ============================================================================


class TestListDevices:
    """Tests for list_devices callable wired through Plugin."""

    @pytest.mark.asyncio
    async def test_list_devices_returns_devices(self, plugin):
        """list_devices callable routes through save service and returns enriched list."""
        plugin._fake_api._registered_devices = [
            {"id": "device-1", "name": "steamdeck"},
        ]
        plugin._save_sync_state.server_device_id = "device-1"

        result = await plugin.list_devices()

        assert result["success"] is True
        assert len(result["devices"]) == 1
        assert result["devices"][0]["is_current_device"] is True

    @pytest.mark.asyncio
    async def test_list_devices_disabled_when_sync_off(self, plugin):
        """Returns disabled=True when save sync is disabled."""
        plugin._save_sync_state.settings.save_sync_enabled = False

        result = await plugin.list_devices()

        assert result["success"] is False
        assert result.get("disabled") is True


# ============================================================================
# Pre-Launch Sync (Plugin callable integration)
# ============================================================================


class TestPreLaunchSync:
    """Tests for pre_launch_sync callable."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, plugin):
        """Returns early when sync_before_launch is false."""
        plugin._save_sync_state.settings.sync_before_launch = False

        result = await plugin.pre_launch_sync(42)

        assert result["synced"] == 0
        assert "disabled" in result["message"].lower()


# ============================================================================
# Post-Exit Sync (Plugin callable integration)
# ============================================================================


class TestPostExitSync:
    """Tests for post_exit_sync callable."""

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, plugin):
        """Returns early when sync_after_exit is false."""
        plugin._save_sync_state.settings.sync_after_exit = False

        result = await plugin.post_exit_sync(42)

        assert result["synced"] == 0
        assert "disabled" in result["message"].lower()

    # ------------------------------------------------------------------
    # Regression tests for issue #238 — post-exit sync must not blow away
    # user progress when a save-sort migration is pending.
    #
    # The two scenarios cover:
    #  1. Mid-session sort change: save was written to the previous layout
    #     during the session that just ended; Rule 1 ensures sync reads
    #     that layout so local progress is uploaded before anything can
    #     touch it.
    #  2. NEW-from-start: the session ran entirely under the new layout
    #     (user changed retroarch.cfg outside of a session then launched
    #     directly via Steam), detect fires at session end, and Rule 2
    #     must prevent sync from downloading stale server content to
    #     the (empty) previous layout — otherwise the mtime-naive
    #     migration resolver would pick that fresh download over the real
    #     user progress at the new layout.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_post_exit_sync_new_from_start_skips_stale_download(self, plugin, tmp_path):
        """NEW-from-start edge: sync must not download stale server content to previous layout (#238)."""
        _install_rom(plugin, tmp_path)
        # Detect just fired at session end. Session ran entirely under the
        # NEW layout because the user had already flipped the setting before
        # launching.
        plugin._state["save_sort_settings"] = {"sort_by_content": False, "sort_by_core": False}
        plugin._state["save_sort_settings_previous"] = {"sort_by_content": True, "sort_by_core": False}

        # Real user progress at the NEW layout (where the session wrote).
        new_save_path = tmp_path / "retrodeck" / "saves" / "pokemon.srm"
        new_save_path.parent.mkdir(parents=True, exist_ok=True)
        new_save_path.write_bytes(b"ACTUAL_USER_PROGRESS")

        # Server has a stale save from a previous device.
        plugin._fake_api.saves[100] = {
            "id": 100,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2020-01-01T00:00:00Z",
            "file_size_bytes": len(b"STALE_SERVER_CONTENT"),
            "emulator": "retroarch",
            "download_path": "/saves/pokemon.srm",
        }
        plugin._fake_api.uploaded_files[100] = str(tmp_path / "server_stale.srm")
        (tmp_path / "server_stale.srm").write_bytes(b"STALE_SERVER_CONTENT")

        # Nothing at the PREVIOUS layout path before sync.
        prev_save_path = tmp_path / "retrodeck" / "saves" / "gba" / "pokemon.srm"
        assert not prev_save_path.exists()

        result = await plugin.post_exit_sync(42)

        assert result["success"] is True
        # No download happened (Rule 2 skipped server_only).
        assert plugin._fake_api.downloaded_files == {}
        # No file created at the PREVIOUS layout path.
        assert not prev_save_path.exists()
        # NEW layout file is untouched — byte-identical to what we wrote.
        assert new_save_path.exists()
        assert new_save_path.read_bytes() == b"ACTUAL_USER_PROGRESS"
        # No upload either — previous layout is empty (nothing to upload).
        upload_calls = [c for c in plugin._fake_api.call_log if c[0] == "upload_save"]
        assert upload_calls == []

    @pytest.mark.asyncio
    async def test_post_exit_sync_detects_stale_state_and_skips_stale_download_c2(self, plugin, tmp_path):
        """End-to-end C2 regression (#238).

        Race scenario: user changes retroarch.cfg outside of a session
        (plugin hasn't detected yet — state still has stale OLD
        settings, no ``previous``). User launches directly via Steam
        (no pre-launch detect). Session runs under NEW layout. Session
        ends. ``post_exit_sync`` arrives at the backend BEFORE
        ``refresh_migration_state`` does.

        Previous behavior: Rule 2 wouldn't engage because ``previous``
        wasn't set yet — sync would compute ``saves_dir`` from the
        stale OLD settings, find it empty, match the server save as
        ``server_only``, download the stale content to the OLD path,
        and the later migration resolver would prefer the
        freshly-downloaded stale file over the real NEW-layout user
        progress.

        Fix: ``post_exit_sync`` calls ``detect_save_sort_change`` at
        the top, which populates ``save_sort_settings_previous``. Rule
        1 then returns the OLD layout for ``_get_rom_save_info``, and
        Rule 2 skips the ``server_only`` match. Real user progress at
        the NEW path stays untouched.
        """
        _install_rom(plugin, tmp_path)

        # Preconditions:
        # - Plugin state thinks save sort is still "sort_by_content only"
        #   (this is what detect LAST wrote — before the user flipped cfg).
        # - No ``previous`` key at all — detect has never seen the change.
        plugin._state["save_sort_settings"] = {
            "sort_by_content": True,
            "sort_by_core": False,
        }
        assert "save_sort_settings_previous" not in plugin._state

        # Real user progress at the NEW layout (where the session wrote).
        # NEW layout: sort_by_content=True + sort_by_core=True adds /mGBA.
        # Simplify: simulate NEW = sort_by_content=False (no gba/ subdir).
        new_save_path = tmp_path / "retrodeck" / "saves" / "pokemon.srm"
        new_save_path.parent.mkdir(parents=True, exist_ok=True)
        new_save_path.write_bytes(b"ACTUAL_USER_PROGRESS")

        # Server has stale content from an earlier device/session.
        plugin._fake_api.saves[100] = {
            "id": 100,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2020-01-01T00:00:00Z",
            "file_size_bytes": len(b"STALE_SERVER_CONTENT"),
            "emulator": "retroarch",
            "download_path": "/saves/pokemon.srm",
        }
        stale_upload = tmp_path / "server_stale.srm"
        stale_upload.write_bytes(b"STALE_SERVER_CONTENT")
        plugin._fake_api.uploaded_files[100] = str(stale_upload)

        # Wire a REAL MigrationService on the SAME state dict as the
        # plugin's SaveService, then point SaveService's
        # ``detect_sort_change`` at the real bound method.
        # ``get_retroarch_save_sorting`` reports the CURRENT on-disk cfg
        # (NEW: sort_by_content=False, sort_by_core=False) — the mismatch
        # with state is what detect will discover.
        real_migration = MigrationService(
            migration_files=MigrationFileAdapter(),
            config=MigrationServiceConfig(
                state=plugin._state,
                loop=asyncio.get_event_loop(),
                logger=logging.getLogger("test"),
                save_state=plugin._save_state,
                emit=MagicMock(),
                get_bios_files_index=lambda: {},
                get_retroarch_save_sorting=lambda: (False, False),
            ),
        )
        # Sanity: same state object — mutations through migration will be
        # visible to SaveService on the next state read.
        assert real_migration._state is plugin._save_sync_service._state

        plugin._save_sync_service._detect_sort_change = real_migration.detect_save_sort_change

        # Nothing at the PREVIOUS (OLD: sort_by_content=True → gba/) path.
        prev_save_path = tmp_path / "retrodeck" / "saves" / "gba" / "pokemon.srm"
        assert not prev_save_path.exists()

        result = await plugin.post_exit_sync(42)

        assert result["success"] is True

        # 1. detect fired inside post_exit_sync and populated
        #    ``save_sort_settings_previous`` on the shared state dict.
        assert plugin._state["save_sort_settings_previous"] == {
            "sort_by_content": True,
            "sort_by_core": False,
        }
        assert plugin._state["save_sort_settings"] == {
            "sort_by_content": False,
            "sort_by_core": False,
        }

        # 2. NO file was written to the OLD layout path (no stale download).
        assert not prev_save_path.exists()
        # FakeSaveApi records any download — confirm none happened.
        assert plugin._fake_api.downloaded_files == {}

        # 3. The file at NEW layout path is byte-identical to what the
        #    session wrote — not touched, not overwritten.
        assert new_save_path.exists()
        assert new_save_path.read_bytes() == b"ACTUAL_USER_PROGRESS"

        # 4. No upload either: Rule 1 points sync at the OLD layout
        #    (empty), so there's nothing to upload from there. The real
        #    NEW-layout save will be picked up after the user resolves
        #    the migration via the Settings UI.
        upload_calls = [c for c in plugin._fake_api.call_log if c[0] == "upload_save"]
        assert upload_calls == []


# ============================================================================
# Playtime Tracking (Plugin callable integration)
# ============================================================================


class TestPlaytimeTracking:
    """Tests for session playtime recording."""

    @pytest.mark.asyncio
    async def test_session_start_records_timestamp(self, plugin):
        """record_session_start saves start time in playtime dict."""
        result = await plugin.record_session_start(42)

        assert result["success"] is True
        entry = plugin._save_sync_state.playtime["42"]
        assert entry.last_session_start is not None
        # Should be a valid ISO datetime
        datetime.fromisoformat(entry.last_session_start)

    @pytest.mark.asyncio
    async def test_session_end_calculates_delta(self, plugin):
        """record_session_end computes correct duration."""
        start_time = plugin._playtime_service._clock.now() - timedelta(seconds=600)
        plugin._save_sync_state.playtime["42"] = PlaytimeEntry(
            last_session_start=start_time.isoformat(),
        )

        result = await plugin.record_session_end(42)

        assert result["success"] is True
        assert result["duration_sec"] >= 590  # ~600s minus execution time
        assert result["total_seconds"] >= 590

    @pytest.mark.asyncio
    async def test_delta_accumulated(self, plugin):
        """Playtime delta added to existing total."""
        start_time = plugin._playtime_service._clock.now() - timedelta(seconds=300)
        plugin._save_sync_state.playtime["42"] = PlaytimeEntry(
            total_seconds=1000,
            session_count=5,
            last_session_start=start_time.isoformat(),
        )

        await plugin.record_session_end(42)

        total = plugin._save_sync_state.playtime["42"].total_seconds
        assert total >= 1290  # 1000 + ~300

    @pytest.mark.asyncio
    async def test_session_count_incremented(self, plugin):
        """Session count goes up on end."""
        start_time = plugin._playtime_service._clock.now() - timedelta(seconds=10)
        plugin._save_sync_state.playtime["42"] = PlaytimeEntry(
            session_count=5,
            last_session_start=start_time.isoformat(),
        )

        result = await plugin.record_session_end(42)

        assert result["session_count"] == 6

    @pytest.mark.asyncio
    async def test_end_without_start(self, plugin):
        """record_session_end without active session returns failure."""
        result = await plugin.record_session_end(42)

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_session_start_clears_on_end(self, plugin):
        """last_session_start is cleared after session end."""
        start_time = plugin._playtime_service._clock.now() - timedelta(seconds=10)
        plugin._save_sync_state.playtime["42"] = PlaytimeEntry(
            last_session_start=start_time.isoformat(),
        )

        await plugin.record_session_end(42)

        assert plugin._save_sync_state.playtime["42"].last_session_start is None

    @pytest.mark.asyncio
    async def test_duration_clamped_to_24h(self, plugin):
        """Duration clamped to max 24 hours."""
        start_time = plugin._playtime_service._clock.now() - timedelta(hours=48)
        plugin._save_sync_state.playtime = {
            rid: PlaytimeEntry.from_dict(entry)
            for rid, entry in (
                {
                    "42": {
                        "total_seconds": 0,
                        "session_count": 0,
                        "last_session_start": start_time.isoformat(),
                        "last_session_duration_sec": None,
                        "offline_deltas": [],
                    }
                }
            ).items()
        }

        result = await plugin.record_session_end(42)

        assert result["duration_sec"] <= 86400  # 24h max


# ============================================================================
# Get All Playtime (Plugin callable integration)
# ============================================================================


class TestGetAllPlaytime:
    """Tests for get_all_playtime callable."""

    @pytest.mark.asyncio
    async def test_returns_all_playtime_entries(self, plugin):
        """Returns all playtime entries from state."""
        plugin._save_sync_state.playtime = {
            rid: PlaytimeEntry.from_dict(entry)
            for rid, entry in (
                {
                    "42": {"total_seconds": 3000, "session_count": 5},
                    "99": {"total_seconds": 600, "session_count": 1},
                }
            ).items()
        }
        result = await plugin.get_all_playtime()
        assert result["playtime"]["42"]["total_seconds"] == 3000
        assert result["playtime"]["99"]["total_seconds"] == 600

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_playtime(self, plugin):
        """Returns empty dict when no playtime data exists."""
        plugin._save_sync_state.playtime = {}
        result = await plugin.get_all_playtime()
        assert result["playtime"] == {}


# ============================================================================
# Save Sync Settings (Plugin callable integration)
# ============================================================================


class TestSaveSyncSettings:
    """Tests for get/update save sync settings."""

    @pytest.mark.asyncio
    async def test_get_returns_current(self, plugin):
        """Returns current settings."""
        result = await plugin.get_save_sync_settings()

        assert result["save_sync_enabled"] is True
        assert result["sync_before_launch"] is True
        assert result["sync_after_exit"] is True

    @pytest.mark.asyncio
    async def test_update_changes_settings(self, plugin, tmp_path):
        """Updates and persists settings."""
        result = await plugin.update_save_sync_settings(
            {
                "save_sync_enabled": True,
                "sync_before_launch": False,
            }
        )

        assert result["success"] is True
        assert result["settings"]["save_sync_enabled"] is True
        assert result["settings"]["sync_before_launch"] is False
        # sync_after_exit unchanged
        assert result["settings"]["sync_after_exit"] is True

        # Persisted
        path = tmp_path / "save_sync_state.json"
        data = json.loads(path.read_text())
        assert data["settings"]["save_sync_enabled"] is True

    @pytest.mark.asyncio
    async def test_unknown_keys_ignored(self, plugin):
        """Unknown settings keys are silently ignored."""
        result = await plugin.update_save_sync_settings(
            {
                "unknown_key": "value",
                "sync_before_launch": True,
            }
        )

        assert result["success"] is True
        assert result["settings"]["sync_before_launch"] is True
        assert "unknown_key" not in result["settings"]

    @pytest.mark.asyncio
    async def test_boolean_coercion(self, plugin):
        """sync toggles coerced to bool."""
        result = await plugin.update_save_sync_settings(
            {
                "sync_before_launch": 0,
                "sync_after_exit": 1,
            }
        )

        assert result["settings"]["sync_before_launch"] is False
        assert result["settings"]["sync_after_exit"] is True


# ============================================================================
# Manual Sync All (Plugin callable integration)
# ============================================================================


class TestSyncAllSaves:
    """Tests for sync_all_saves."""

    @pytest.mark.asyncio
    async def test_no_installed_roms(self, plugin):
        """Empty installed_roms completes gracefully."""
        plugin._save_sync_state.device_id = "dev-1"

        result = await plugin.sync_all_saves()

        assert result["success"] is True
        assert result["roms_checked"] == 0
        assert result["synced"] == 0


# ============================================================================
# Single ROM Sync (Plugin callable integration)
# ============================================================================


class TestSyncRomSaves:
    """Tests for sync_rom_saves callable (bidirectional per-ROM sync)."""

    @pytest.mark.asyncio
    async def test_rom_not_installed(self, plugin):
        """Non-installed ROM returns 0 synced."""
        plugin._save_sync_state.device_id = "dev-1"

        result = await plugin.sync_rom_saves(999)

        assert result["success"] is True
        assert result["synced"] == 0


# ============================================================================
# Retry Logic (MRO verification)
# ============================================================================


class TestRetryMRO:
    """Verify with_retry is accessible on Plugin via _http_adapter."""

    def test_with_retry_accessible_via_http_adapter(self, plugin):
        """with_retry should be accessible via _http_adapter."""
        fn = MagicMock(return_value="ok")
        result = plugin._http_adapter.with_retry(fn, "arg1")
        assert result == "ok"
        fn.assert_called_once_with("arg1")


# ============================================================================
# Feature Flag: save_sync_enabled (Plugin callable integration)
# ============================================================================


class TestSaveSyncFeatureFlag:
    """Tests for the save_sync_enabled feature flag (off by default)."""

    @pytest.mark.asyncio
    async def test_default_disabled(self, plugin):
        """save_sync_enabled defaults to False in fresh state."""
        # Reset to defaults (no test fixture override).
        plugin._save_sync_state.replace_with(SaveService.make_default_state())
        assert plugin._save_sync_state.settings.save_sync_enabled is False

    @pytest.mark.asyncio
    async def test_ensure_device_disabled(self, plugin):
        """ensure_device_registered returns disabled marker when save sync off."""
        plugin._save_sync_state.settings.save_sync_enabled = False
        result = await plugin.ensure_device_registered()
        assert result["success"] is False
        assert result.get("disabled") is True
        assert plugin._save_sync_state.device_id is None

    @pytest.mark.asyncio
    async def test_pre_launch_sync_disabled(self, plugin):
        """pre_launch_sync skips when save sync disabled."""
        plugin._save_sync_state.settings.save_sync_enabled = False
        result = await plugin.pre_launch_sync(42)
        assert result["success"] is True
        assert result["synced"] == 0
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_post_exit_sync_disabled(self, plugin):
        """post_exit_sync skips when save sync disabled."""
        plugin._save_sync_state.settings.save_sync_enabled = False
        result = await plugin.post_exit_sync(42)
        assert result["success"] is True
        assert result["synced"] == 0
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_sync_rom_saves_disabled(self, plugin):
        """sync_rom_saves returns error when save sync disabled."""
        plugin._save_sync_state.settings.save_sync_enabled = False
        result = await plugin.sync_rom_saves(42)
        assert result["success"] is False
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_sync_all_saves_disabled(self, plugin):
        """sync_all_saves returns error when save sync disabled."""
        plugin._save_sync_state.settings.save_sync_enabled = False
        result = await plugin.sync_all_saves()
        assert result["success"] is False
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_enable_via_settings_update(self, plugin):
        """save_sync_enabled can be toggled via update_save_sync_settings."""
        plugin._save_sync_state.settings.save_sync_enabled = False
        result = await plugin.update_save_sync_settings({"save_sync_enabled": True})
        assert result["success"] is True
        assert plugin._save_sync_state.settings.save_sync_enabled is True

    @pytest.mark.asyncio
    async def test_disable_via_settings_update(self, plugin):
        """save_sync_enabled can be disabled via update_save_sync_settings."""
        result = await plugin.update_save_sync_settings({"save_sync_enabled": False})
        assert result["success"] is True
        assert plugin._save_sync_state.settings.save_sync_enabled is False

    @pytest.mark.asyncio
    async def test_get_settings_includes_flag(self, plugin):
        """get_save_sync_settings returns save_sync_enabled field."""
        result = await plugin.get_save_sync_settings()
        assert "save_sync_enabled" in result

    @pytest.mark.asyncio
    async def test_is_save_sync_enabled_helper(self, plugin):
        """_is_save_sync_enabled reflects the settings value."""
        plugin._save_sync_state.settings.save_sync_enabled = True
        assert plugin._save_sync_service._is_save_sync_enabled() is True
        plugin._save_sync_state.settings.save_sync_enabled = False
        assert plugin._save_sync_service._is_save_sync_enabled() is False


# ============================================================================
# Delete Local Saves (Plugin callable integration)
# ============================================================================


@pytest.mark.asyncio
async def test_delete_local_saves_happy_path(plugin, tmp_path):
    """Deleting local saves removes files and cleans sync state."""
    rom_id = 100
    system = "snes"
    rom_name = "TestGame"

    # Register as installed (file_path needed for _get_rom_save_info)
    plugin._state["installed_roms"]["100"] = {
        "rom_id": 100,
        "file_name": f"{rom_name}.sfc",
        "file_path": str(tmp_path / "retrodeck" / "roms" / system / f"{rom_name}.sfc"),
        "system": system,
        "platform_slug": "snes",
    }

    # Create fake save files in the fallback saves path
    saves_dir = tmp_path / "retrodeck" / "saves" / system
    saves_dir.mkdir(parents=True)
    srm = saves_dir / f"{rom_name}.srm"
    rtc = saves_dir / f"{rom_name}.rtc"
    srm.write_bytes(b"\x00" * 32)
    rtc.write_bytes(b"\x00" * 16)

    # Set up sync state
    plugin._save_sync_state.saves["100"] = RomSaveState.from_dict(
        {
            "files": {
                f"{rom_name}.srm": {"last_sync_hash": "abc123"},
                f"{rom_name}.rtc": {"last_sync_hash": "def456"},
            },
            "system": system,
        }
    )

    result = await plugin.delete_local_saves(rom_id)
    assert result["success"] is True
    assert result["deleted_count"] == 2
    assert not srm.exists()
    assert not rtc.exists()
    # Entry survives — only files are cleared (#279).
    assert "100" in plugin._save_sync_state.saves
    assert plugin._save_sync_state.saves["100"].files == {}
    assert plugin._save_sync_state.saves["100"].system == system


@pytest.mark.asyncio
async def test_delete_local_saves_preserves_slot_config(plugin, tmp_path):
    """Slot config / attribution metadata survive a delete (#279)."""
    rom_id = 101
    system = "snes"
    rom_name = "SlotGame"

    plugin._state["installed_roms"]["101"] = {
        "rom_id": 101,
        "file_name": f"{rom_name}.sfc",
        "file_path": str(tmp_path / "retrodeck" / "roms" / system / f"{rom_name}.sfc"),
        "system": system,
        "platform_slug": "snes",
    }

    saves_dir = tmp_path / "retrodeck" / "saves" / system
    saves_dir.mkdir(parents=True)
    srm = saves_dir / f"{rom_name}.srm"
    srm.write_bytes(b"\x00" * 32)

    plugin._save_sync_state.saves["101"] = RomSaveState.from_dict(
        {
            "files": {f"{rom_name}.srm": {"last_sync_hash": "hash"}},
            "active_slot": "desktop",
            "slot_confirmed": True,
            "emulator": "retroarch-snes9x",
            "last_synced_core": "snes9x_libretro",
            "own_upload_ids": ["save-9"],
            "slots": {"default": {}, "desktop": {}},
            "system": system,
        }
    )

    result = await plugin.delete_local_saves(rom_id)
    assert result["success"] is True
    assert result["deleted_count"] == 1
    assert not srm.exists()

    entry = plugin._save_sync_state.saves["101"]
    assert entry.files == {}
    assert entry.active_slot == "desktop"
    assert entry.slot_confirmed is True
    assert entry.emulator == "retroarch-snes9x"
    assert entry.last_synced_core == "snes9x_libretro"
    assert entry.own_upload_ids == ["save-9"]
    assert entry.slots == {"default": {}, "desktop": {}}
    assert entry.system == system


@pytest.mark.asyncio
async def test_delete_local_saves_no_files(plugin, tmp_path):
    """Deleting saves when none exist returns success with 0."""
    plugin._state["installed_roms"]["200"] = {
        "rom_id": 200,
        "file_name": "NoSaves.sfc",
        "file_path": str(tmp_path / "retrodeck" / "roms" / "snes" / "NoSaves.sfc"),
        "system": "snes",
        "platform_slug": "snes",
    }

    result = await plugin.delete_local_saves(200)
    assert result["success"] is True
    assert result["deleted_count"] == 0


@pytest.mark.asyncio
async def test_delete_local_saves_not_installed(plugin):
    """Deleting saves for a non-installed ROM returns success with 0."""
    result = await plugin.delete_local_saves(999)
    assert result["success"] is True
    assert result["deleted_count"] == 0


# ============================================================================
# Delete Platform Saves (Plugin callable integration)
# ============================================================================


@pytest.mark.asyncio
async def test_delete_platform_saves(plugin, tmp_path):
    """Deleting platform saves removes files for all ROMs on that platform."""
    saves_dir = tmp_path / "retrodeck" / "saves" / "snes"
    saves_dir.mkdir(parents=True)

    srm1 = saves_dir / "Game1.srm"
    srm2 = saves_dir / "Game2.srm"
    srm1.write_bytes(b"\x00" * 32)
    srm2.write_bytes(b"\x00" * 32)

    plugin._state["installed_roms"]["10"] = {
        "rom_id": 10,
        "file_name": "Game1.sfc",
        "file_path": str(tmp_path / "retrodeck" / "roms" / "snes" / "Game1.sfc"),
        "system": "snes",
        "platform_slug": "snes",
    }
    plugin._state["installed_roms"]["20"] = {
        "rom_id": 20,
        "file_name": "Game2.sfc",
        "file_path": str(tmp_path / "retrodeck" / "roms" / "snes" / "Game2.sfc"),
        "system": "snes",
        "platform_slug": "snes",
    }
    plugin._state["installed_roms"]["30"] = {
        "rom_id": 30,
        "file_name": "GBAGame.gba",
        "file_path": str(tmp_path / "retrodeck" / "roms" / "gba" / "GBAGame.gba"),
        "system": "gba",
        "platform_slug": "gba",
    }

    plugin._save_sync_state.saves["10"] = RomSaveState(files={"Game1.srm": FileSyncState()}, system="snes")
    plugin._save_sync_state.saves["20"] = RomSaveState(files={"Game2.srm": FileSyncState()}, system="snes")

    result = await plugin.delete_platform_saves("snes")
    assert result["success"] is True
    assert result["deleted_count"] == 2
    assert not srm1.exists()
    assert not srm2.exists()
    # Entries survive — only files are cleared (#279).
    assert "10" in plugin._save_sync_state.saves
    assert plugin._save_sync_state.saves["10"].files == {}
    assert plugin._save_sync_state.saves["10"].system == "snes"
    assert "20" in plugin._save_sync_state.saves
    assert plugin._save_sync_state.saves["20"].files == {}
    assert plugin._save_sync_state.saves["20"].system == "snes"


# ============================================================================
# Version History callables (plugin-level integration)
# ============================================================================


class TestSavesVersionHistoryCallables:
    """Integration tests for version history callables."""

    @pytest.mark.asyncio
    async def test_saves_list_file_versions_happy_path(self, plugin, tmp_path):
        """saves_list_file_versions returns filtered older versions."""
        plugin._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "files": {"pokemon.srm": {"tracked_save_id": 100}},
            }
        )
        plugin._fake_api.saves[100] = {
            "id": 100,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2026-03-10T00:00:00Z",
            "file_size_bytes": 1024,
            "slot": "default",
            "download_path": "/saves/pokemon.srm",
        }
        plugin._fake_api.saves[50] = {
            "id": 50,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2026-03-01T00:00:00Z",
            "file_size_bytes": 512,
            "slot": "default",
            "download_path": "/saves/pokemon.srm",
        }

        result = await plugin.saves_list_file_versions(42, "default", "pokemon.srm")

        assert len(result) == 1
        assert result[0]["id"] == 50

    @pytest.mark.asyncio
    async def test_saves_rollback_to_version_happy_path(self, plugin, tmp_path):
        """saves_rollback_to_version downloads the target save on success."""
        _install_rom(plugin, tmp_path)

        # Create local save file with content matching last_sync_hash
        saves_dir = tmp_path / "retrodeck" / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)
        save_file = saves_dir / "pokemon.srm"
        save_file.write_bytes(b"\x00" * 1024)

        import hashlib

        local_hash = hashlib.md5(b"\x00" * 1024).hexdigest()

        plugin._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "files": {"pokemon.srm": {"tracked_save_id": 100, "last_sync_hash": local_hash}},
            }
        )
        plugin._fake_api.saves[100] = {
            "id": 100,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2026-03-10T00:00:00Z",
            "file_size_bytes": 1024,
            "slot": "default",
            "download_path": "/saves/pokemon.srm",
        }
        plugin._fake_api.saves[50] = {
            "id": 50,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2026-03-01T00:00:00Z",
            "file_size_bytes": 1024,
            "slot": "default",
            "download_path": "/saves/pokemon.srm",
        }

        result = await plugin.saves_rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        download_calls = [c for c in plugin._fake_api.call_log if c[0] == "download_save_content"]
        assert any(c[1][0] == 50 for c in download_calls)

    @pytest.mark.asyncio
    async def test_saves_rollback_to_version_signature(self, plugin):
        """saves_rollback_to_version's signature is (rom_id, slot, save_id) —
        no force flag (matrix pre-flight replaced Gate D/F) and no filename
        (the canonical local path is derived from the target save + ROM)."""
        import inspect

        sig = inspect.signature(plugin.saves_rollback_to_version)
        params = list(sig.parameters.keys())
        assert params == ["rom_id", "slot", "save_id"]
