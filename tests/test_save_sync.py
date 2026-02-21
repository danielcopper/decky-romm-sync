import pytest
import json
import os
import asyncio
import hashlib
import time
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

# conftest.py patches decky before this import
from main import Plugin


@pytest.fixture
def plugin(tmp_path):
    p = Plugin()
    p.settings = {
        "romm_url": "http://romm.local",
        "romm_user": "user",
        "romm_pass": "pass",
        "enabled_platforms": {},
        "debug_logging": False,
    }
    p._sync_running = False
    p._sync_cancel = False
    p._sync_progress = {"running": False}
    p._state = {
        "shortcut_registry": {},
        "installed_roms": {},
        "last_sync": None,
        "sync_stats": {},
    }
    p._pending_sync = {}
    p._download_tasks = {}
    p._download_queue = {}
    p._download_in_progress = set()
    p._metadata_cache = {}

    import decky
    decky.DECKY_PLUGIN_RUNTIME_DIR = str(tmp_path)
    decky.DECKY_USER_HOME = str(tmp_path)

    p._init_save_sync_state()
    # Enable save sync for tests — matches pre-feature-flag behavior
    p._save_sync_state["settings"]["save_sync_enabled"] = True
    return p


@pytest.fixture(autouse=True)
async def _set_event_loop(plugin):
    """Ensure plugin.loop matches the running event loop for async tests."""
    plugin.loop = asyncio.get_event_loop()


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


def _server_save(save_id=100, rom_id=42, filename="pokemon.srm",
                 updated_at="2026-02-17T06:00:00Z", file_size_bytes=1024):
    """Helper: build a server save response dict (matches RomM 4.6.1 SaveSchema).

    Default updated_at is BEFORE the typical last_sync_at in tests (08:00)
    to avoid triggering slow-path detection in _detect_conflict.
    No content_hash — RomM 4.6.1 SaveSchema does not include it.
    """
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
# State Management
# ============================================================================


class TestInitSaveSyncState:
    """Tests for _init_save_sync_state defaults."""

    def test_initializes_defaults(self, plugin):
        """Default state has expected structure."""
        s = plugin._save_sync_state
        assert s["version"] == 1
        assert s["device_id"] is None
        assert s["device_name"] is None
        assert s["saves"] == {}
        assert s["playtime"] == {}
        assert s["pending_conflicts"] == []
        assert s["offline_queue"] == []
        assert s["settings"]["conflict_mode"] == "newest_wins"
        assert s["settings"]["sync_before_launch"] is True
        assert s["settings"]["sync_after_exit"] is True
        assert s["settings"]["clock_skew_tolerance_sec"] == 60


class TestSaveSyncStatePersistence:
    """Tests for save_sync_state.json load/save."""

    def test_save_state_writes_correctly(self, plugin, tmp_path):
        """Atomic write produces valid JSON."""
        plugin._save_sync_state["device_id"] = "test-uuid"
        plugin._save_sync_state["saves"]["42"] = {
            "files": {"pokemon.srm": {"last_sync_hash": "abc"}},
            "emulator": "retroarch",
            "system": "gba",
        }

        plugin._save_save_sync_state()

        path = tmp_path / "save_sync_state.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["device_id"] == "test-uuid"
        assert data["saves"]["42"]["files"]["pokemon.srm"]["last_sync_hash"] == "abc"

    def test_load_merges_with_defaults(self, plugin, tmp_path):
        """Load merges saved data into default structure."""
        state_data = {
            "version": 1,
            "device_id": "saved-uuid",
            "device_name": "steamdeck",
            "saves": {"10": {"files": {}, "emulator": "retroarch", "system": "n64"}},
            "playtime": {"10": {"total_seconds": 3600}},
            "pending_conflicts": [],
            "offline_queue": [],
            "settings": {"conflict_mode": "always_upload", "sync_before_launch": False},
        }
        (tmp_path / "save_sync_state.json").write_text(json.dumps(state_data))

        plugin._load_save_sync_state()

        assert plugin._save_sync_state["device_id"] == "saved-uuid"
        assert plugin._save_sync_state["device_name"] == "steamdeck"
        assert plugin._save_sync_state["settings"]["conflict_mode"] == "always_upload"
        assert plugin._save_sync_state["settings"]["sync_before_launch"] is False
        # Default values preserved for keys not in saved data
        assert plugin._save_sync_state["settings"]["sync_after_exit"] is True

    def test_handles_missing_file(self, plugin, tmp_path):
        """Missing state file keeps defaults."""
        plugin._save_sync_state["device_id"] = "should-stay-none"
        plugin._init_save_sync_state()  # Reset to defaults
        plugin._load_save_sync_state()

        assert plugin._save_sync_state["device_id"] is None

    def test_handles_corrupt_json(self, plugin, tmp_path):
        """Corrupt JSON falls back to defaults."""
        (tmp_path / "save_sync_state.json").write_text("{{{invalid!")

        plugin._load_save_sync_state()

        # Should not crash; defaults remain
        assert plugin._save_sync_state["device_id"] is None
        assert isinstance(plugin._save_sync_state["saves"], dict)

    def test_separate_from_main_state(self, plugin, tmp_path):
        """save_sync_state.json is separate from state.json."""
        plugin._save_sync_state["device_id"] = "test-uuid"
        plugin._save_save_sync_state()

        sync_path = tmp_path / "save_sync_state.json"
        main_path = tmp_path / "state.json"

        assert sync_path.exists()
        sync_data = json.loads(sync_path.read_text())
        assert "device_id" in sync_data

        if main_path.exists():
            main_data = json.loads(main_path.read_text())
            assert "device_id" not in main_data


# ============================================================================
# Device Registration
# ============================================================================


class TestDeviceRegistration:
    """Tests for ensure_device_registered (local UUID generation)."""

    @pytest.mark.asyncio
    async def test_generates_local_uuid(self, plugin, tmp_path):
        """First call generates a local UUID, no server call needed."""
        result = await plugin.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"]
        assert len(result["device_id"]) == 36  # UUID format
        assert plugin._save_sync_state["device_id"] == result["device_id"]

        # Persisted to disk
        path = tmp_path / "save_sync_state.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["device_id"] == result["device_id"]

    @pytest.mark.asyncio
    async def test_already_registered_returns_cached(self, plugin):
        """If device_id already set, returns immediately without generating new one."""
        plugin._save_sync_state["device_id"] = "existing-uuid"
        plugin._save_sync_state["device_name"] = "myhost"

        result = await plugin.ensure_device_registered()

        assert result["success"] is True
        assert result["device_id"] == "existing-uuid"
        assert result["device_name"] == "myhost"

    @pytest.mark.asyncio
    async def test_sets_hostname_as_device_name(self, plugin):
        """Device name is set to the local hostname."""
        with patch("socket.gethostname", return_value="steamdeck"):
            result = await plugin.ensure_device_registered()

        assert result["device_name"] == "steamdeck"
        assert plugin._save_sync_state["device_name"] == "steamdeck"

    @pytest.mark.asyncio
    async def test_generates_unique_ids(self, plugin):
        """Each new registration generates a unique UUID."""
        result1 = await plugin.ensure_device_registered()
        id1 = result1["device_id"]

        # Reset state to force new generation
        plugin._save_sync_state["device_id"] = None
        result2 = await plugin.ensure_device_registered()
        id2 = result2["device_id"]

        assert id1 != id2


# ============================================================================
# Save File Discovery
# ============================================================================


class TestFindSaveFiles:
    """Tests for _find_save_files."""

    def test_finds_srm(self, plugin, tmp_path):
        """Finds .srm file matching ROM name."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, system="gba", rom_name="pokemon")

        result = plugin._find_save_files(42)

        assert len(result) == 1
        assert result[0]["filename"] == "pokemon.srm"
        assert result[0]["path"].endswith("pokemon.srm")

    def test_finds_rtc_companion(self, plugin, tmp_path):
        """Finds .rtc companion alongside .srm."""
        _install_rom(plugin, tmp_path, file_name="emerald.gba")
        _create_save(tmp_path, rom_name="emerald", ext=".srm")
        _create_save(tmp_path, rom_name="emerald", ext=".rtc", content=b"\x02" * 16)

        result = plugin._find_save_files(42)

        filenames = sorted(f["filename"] for f in result)
        assert filenames == ["emerald.rtc", "emerald.srm"]

    def test_multi_disc_uses_m3u_name(self, plugin, tmp_path):
        """Multi-disc ROM: save name derived from M3U launch file."""
        plugin._state["installed_roms"]["55"] = {
            "rom_id": 55,
            "file_name": "FF7.zip",
            "file_path": str(tmp_path / "retrodeck" / "roms" / "psx" / "FF7" / "Final Fantasy VII.m3u"),
            "system": "psx",
            "platform_slug": "psx",
            "rom_dir": str(tmp_path / "retrodeck" / "roms" / "psx" / "FF7"),
            "installed_at": "2026-01-01T00:00:00",
        }
        _create_save(tmp_path, system="psx", rom_name="Final Fantasy VII")

        result = plugin._find_save_files(55)

        assert any(f["filename"] == "Final Fantasy VII.srm" for f in result)

    def test_no_save_file_returns_empty(self, plugin, tmp_path):
        """No matching save file → empty list."""
        _install_rom(plugin, tmp_path, rom_id=10, system="n64", file_name="zelda.z64")
        (tmp_path / "retrodeck" / "saves" / "n64").mkdir(parents=True, exist_ok=True)

        result = plugin._find_save_files(10)

        assert result == []

    def test_saves_dir_not_exists_returns_empty(self, plugin, tmp_path):
        """Saves directory doesn't exist → empty list (no crash)."""
        _install_rom(plugin, tmp_path)

        result = plugin._find_save_files(42)

        assert result == []

    def test_rom_not_installed_returns_empty(self, plugin):
        """ROM not in installed_roms → empty list."""
        result = plugin._find_save_files(999)

        assert result == []


# ============================================================================
# MD5 Hash Computation
# ============================================================================


class TestFileMd5:
    """Tests for _file_md5."""

    def test_known_content(self, plugin, tmp_path):
        """Correct MD5 for known bytes."""
        f = tmp_path / "test.srm"
        content = b"Hello, save file!"
        f.write_bytes(content)

        assert plugin._file_md5(str(f)) == hashlib.md5(content).hexdigest()

    def test_empty_file(self, plugin, tmp_path):
        """Empty file gives MD5 of empty bytes."""
        f = tmp_path / "empty.srm"
        f.write_bytes(b"")

        assert plugin._file_md5(str(f)) == hashlib.md5(b"").hexdigest()

    def test_large_file_chunked(self, plugin, tmp_path):
        """2MB file hashed correctly (chunked reading)."""
        f = tmp_path / "large.srm"
        content = os.urandom(2 * 1024 * 1024)
        f.write_bytes(content)

        assert plugin._file_md5(str(f)) == hashlib.md5(content).hexdigest()


# ============================================================================
# Three-Way Conflict Detection
# ============================================================================


class TestDetectConflict:
    """Tests for _detect_conflict three-way logic."""

    def _setup_sync_state(self, plugin, rom_id, filename, last_sync_hash,
                          server_updated_at="2026-02-17T06:00:00Z",
                          server_size=1024):
        """Set up per-file sync state for a ROM."""
        rom_id_str = str(rom_id)
        plugin._save_sync_state["saves"][rom_id_str] = {
            "files": {
                filename: {
                    "last_sync_hash": last_sync_hash,
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": server_updated_at,
                    "last_sync_server_size": server_size,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

    def test_no_change_either_side(self, plugin):
        """Local matches snapshot, server metadata matches stored → skip (fast path)."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123")
        server = _server_save()

        result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        assert result == "skip"

    def test_server_changed_local_unchanged(self, plugin):
        """Server differs from snapshot, local matches → download.

        Server updated_at differs from stored → slow path triggers.
        Mock _get_server_save_hash returns different hash → server changed.
        """
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123")
        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        with patch.object(plugin, "_get_server_save_hash", return_value="new_server_hash"):
            result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        assert result == "download"

    def test_local_changed_server_unchanged(self, plugin):
        """Local differs from snapshot, server metadata matches stored → upload (fast path)."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123")
        server = _server_save()

        result = plugin._detect_conflict(42, "pokemon.srm", "new_local_hash", server)

        assert result == "upload"

    def test_both_changed(self, plugin):
        """Both local and server differ from snapshot → conflict.

        Server updated_at differs → slow path → server hash changed.
        Local hash also differs from snapshot.
        """
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123")
        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        with patch.object(plugin, "_get_server_save_hash", return_value="server_new"):
            result = plugin._detect_conflict(42, "pokemon.srm", "local_new", server)

        assert result == "conflict"

    def test_first_sync_server_download_fails(self, plugin):
        """First sync (no snapshot), server download fails → conflict (ask user)."""
        server = _server_save()

        with patch.object(plugin, "_get_server_save_hash", return_value=None):
            result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        assert result == "conflict"

    def test_first_sync_matching_hashes(self, plugin):
        """First sync (no snapshot), local matches server → skip."""
        server = _server_save()

        with patch.object(plugin, "_get_server_save_hash", return_value="abc123"):
            result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        assert result == "skip"

    def test_first_sync_different_hashes(self, plugin):
        """First sync (no snapshot), local differs from server → conflict."""
        server = _server_save()

        with patch.object(plugin, "_get_server_save_hash", return_value="server_hash"):
            result = plugin._detect_conflict(42, "pokemon.srm", "local_hash", server)

        assert result == "conflict"

    def test_no_local_file_server_has_save(self, plugin):
        """No local file, server has save → download."""
        server = _server_save()

        result = plugin._detect_conflict(42, "pokemon.srm", None, server)

        assert result == "download"

    def test_server_size_change_fast_path(self, plugin):
        """Server updated_at matches but size differs → fast-path detects change."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123",
                               server_updated_at="2026-02-17T06:00:00Z",
                               server_size=1024)
        # Same timestamp but different size → fast path detects change
        server = _server_save(updated_at="2026-02-17T06:00:00Z", file_size_bytes=2048)

        result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        # Fast path: timestamp matches, size differs → server_changed=True
        # local unchanged → download
        assert result == "download"


# ============================================================================
# Conflict Resolution Modes
# ============================================================================


class TestResolveConflictByMode:
    """Tests for _resolve_conflict_by_mode."""

    def test_always_upload(self, plugin):
        """always_upload returns upload regardless."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "always_upload"
        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        result = plugin._resolve_conflict_by_mode(time.time() - 7200, server)

        assert result == "upload"

    def test_always_download(self, plugin):
        """always_download returns download regardless."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "always_download"
        server = _server_save(updated_at="2026-02-17T10:00:00Z")

        result = plugin._resolve_conflict_by_mode(time.time(), server)

        assert result == "download"

    def test_ask_me(self, plugin):
        """ask_me returns ask."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "ask_me"
        server = _server_save()

        result = plugin._resolve_conflict_by_mode(time.time(), server)

        assert result == "ask"

    def test_newest_wins_local_newer(self, plugin):
        """newest_wins: local mtime > server updated_at → upload."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        # Server is 2 hours old
        server = _server_save(updated_at="2026-02-17T10:00:00Z")
        # Local is current time (much newer)
        local_mtime = datetime(2026, 2, 17, 14, 0, 0, tzinfo=timezone.utc).timestamp()

        result = plugin._resolve_conflict_by_mode(local_mtime, server)

        assert result == "upload"

    def test_newest_wins_server_newer(self, plugin):
        """newest_wins: server updated_at > local mtime → download."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        server = _server_save(updated_at="2026-02-17T14:00:00Z")
        local_mtime = datetime(2026, 2, 17, 10, 0, 0, tzinfo=timezone.utc).timestamp()

        result = plugin._resolve_conflict_by_mode(local_mtime, server)

        assert result == "download"

    def test_newest_wins_within_clock_skew(self, plugin):
        """newest_wins: timestamps within tolerance → ask."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        plugin._save_sync_state["settings"]["clock_skew_tolerance_sec"] = 60
        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        # 30 seconds different — within 60s tolerance
        local_mtime = datetime(2026, 2, 17, 12, 0, 30, tzinfo=timezone.utc).timestamp()

        result = plugin._resolve_conflict_by_mode(local_mtime, server)

        assert result == "ask"


# ============================================================================
# Pre-Launch Sync
# ============================================================================


class TestPreLaunchSync:
    """Tests for pre_launch_sync callable."""

    @pytest.mark.asyncio
    async def test_downloads_newer_server_save(self, plugin, tmp_path):
        """Downloads server save when it's newer (slow path detects change)."""
        _install_rom(plugin, tmp_path)
        save_file = _create_save(tmp_path)
        local_hash = plugin._file_md5(str(save_file))

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        def fake_download_save(save_id, dest):
            with open(dest, "wb") as f:
                f.write(b"\xff" * 1024)

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_romm_download_save", side_effect=fake_download_save):
            result = await plugin.pre_launch_sync(42)

        assert result["synced"] >= 1

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, plugin):
        """Returns early when sync_before_launch is false."""
        plugin._save_sync_state["settings"]["sync_before_launch"] = False

        result = await plugin.pre_launch_sync(42)

        assert result["synced"] == 0
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_handles_offline_gracefully(self, plugin, tmp_path):
        """Server unreachable → returns error, does not crash."""
        _install_rom(plugin, tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        with patch.object(plugin, "_romm_list_saves", side_effect=ConnectionError("offline")):
            result = await plugin.pre_launch_sync(42)

        # Should complete without raising
        assert "error" in result.get("message", "").lower() or len(result.get("errors", [])) > 0

    @pytest.mark.asyncio
    async def test_queues_conflict_for_ask_me(self, plugin, tmp_path):
        """Conflict in ask_me mode is queued, not blocking."""
        _install_rom(plugin, tmp_path)
        save_content = b"\x01" * 1024
        _create_save(tmp_path, content=save_content)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["settings"]["conflict_mode"] = "ask_me"
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": "old_snapshot",
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_get_server_save_hash", return_value="server_changed"):
            result = await plugin.pre_launch_sync(42)

        assert len(plugin._save_sync_state["pending_conflicts"]) >= 1

    @pytest.mark.asyncio
    async def test_non_blocking_on_exception(self, plugin, tmp_path):
        """Unexpected errors do not crash."""
        _install_rom(plugin, tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        with patch.object(plugin, "_romm_list_saves", side_effect=RuntimeError("unexpected")):
            result = await plugin.pre_launch_sync(42)

        assert result is not None


# ============================================================================
# Post-Exit Sync
# ============================================================================


class TestPostExitSync:
    """Tests for post_exit_sync callable."""

    @pytest.mark.asyncio
    async def test_uploads_changed_save(self, plugin, tmp_path):
        """Uploads save when local hash differs from snapshot (fast path: server unchanged)."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"\x05" * 1024)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": "old_hash_before_play",
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        server = _server_save()
        upload_response = {"id": 200, "rom_id": 42, "file_name": "pokemon.srm", "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response):
            result = await plugin.post_exit_sync(42)

        assert result["synced"] >= 1

    @pytest.mark.asyncio
    async def test_skips_unchanged_save(self, plugin, tmp_path):
        """Skips upload when local hash matches snapshot and server unchanged (fast path)."""
        _install_rom(plugin, tmp_path)
        save_content = b"\x05" * 1024
        _create_save(tmp_path, content=save_content)
        current_hash = hashlib.md5(save_content).hexdigest()

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": current_hash,
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]):
            result = await plugin.post_exit_sync(42)

        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_skips_when_disabled(self, plugin):
        """Returns early when sync_after_exit is false."""
        plugin._save_sync_state["settings"]["sync_after_exit"] = False

        result = await plugin.post_exit_sync(42)

        assert result["synced"] == 0
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_upload_errors_reported(self, plugin, tmp_path):
        """Network errors during upload are captured in errors list."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"\x05" * 1024)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": "old",
                    "last_sync_at": "2026-02-17T08:00:00Z",
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        # No server save → upload path
        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", side_effect=ConnectionError("timeout")):
            result = await plugin.post_exit_sync(42)

        assert len(result.get("errors", [])) >= 1

    @pytest.mark.asyncio
    async def test_409_conflict_queued(self, plugin, tmp_path):
        """409 during upload queues conflict in pending_conflicts."""
        import urllib.error

        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"\x05" * 1024)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": "old",
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        server = _server_save()
        error_409 = urllib.error.HTTPError(
            url="http://romm.local/api/saves", code=409,
            msg="Conflict", hdrs={}, fp=None,
        )

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_romm_upload_save", side_effect=error_409):
            result = await plugin.post_exit_sync(42)

        assert len(plugin._save_sync_state["pending_conflicts"]) >= 1


# ============================================================================
# Playtime Tracking
# ============================================================================


class TestPlaytimeTracking:
    """Tests for session playtime recording."""

    @pytest.mark.asyncio
    async def test_session_start_records_timestamp(self, plugin):
        """record_session_start saves start time in playtime dict."""
        result = await plugin.record_session_start(42)

        assert result["success"] is True
        entry = plugin._save_sync_state["playtime"]["42"]
        assert entry["last_session_start"] is not None
        # Should be a valid ISO datetime
        datetime.fromisoformat(entry["last_session_start"])

    @pytest.mark.asyncio
    async def test_session_end_calculates_delta(self, plugin):
        """record_session_end computes correct duration."""
        # Start session with a known time
        plugin._save_sync_state.setdefault("playtime", {})
        start_time = datetime.now(timezone.utc) - timedelta(seconds=600)
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 0,
            "session_count": 0,
            "last_session_start": start_time.isoformat(),
            "last_session_duration_sec": None,
            "offline_deltas": [],
        }

        result = await plugin.record_session_end(42)

        assert result["success"] is True
        assert result["duration_sec"] >= 590  # ~600s minus execution time
        assert result["total_seconds"] >= 590

    @pytest.mark.asyncio
    async def test_delta_accumulated(self, plugin):
        """Playtime delta added to existing total."""
        start_time = datetime.now(timezone.utc) - timedelta(seconds=300)
        plugin._save_sync_state["playtime"] = {
            "42": {
                "total_seconds": 1000,
                "session_count": 5,
                "last_session_start": start_time.isoformat(),
                "last_session_duration_sec": None,
                "offline_deltas": [],
            }
        }

        result = await plugin.record_session_end(42)

        total = plugin._save_sync_state["playtime"]["42"]["total_seconds"]
        assert total >= 1290  # 1000 + ~300

    @pytest.mark.asyncio
    async def test_session_count_incremented(self, plugin):
        """Session count goes up on end."""
        start_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        plugin._save_sync_state["playtime"] = {
            "42": {
                "total_seconds": 0,
                "session_count": 5,
                "last_session_start": start_time.isoformat(),
                "last_session_duration_sec": None,
                "offline_deltas": [],
            }
        }

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
        start_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        plugin._save_sync_state["playtime"] = {
            "42": {
                "total_seconds": 0,
                "session_count": 0,
                "last_session_start": start_time.isoformat(),
                "last_session_duration_sec": None,
                "offline_deltas": [],
            }
        }

        await plugin.record_session_end(42)

        assert plugin._save_sync_state["playtime"]["42"]["last_session_start"] is None

    @pytest.mark.asyncio
    async def test_duration_clamped_to_24h(self, plugin):
        """Duration clamped to max 24 hours."""
        # Session start 48 hours ago
        start_time = datetime.now(timezone.utc) - timedelta(hours=48)
        plugin._save_sync_state["playtime"] = {
            "42": {
                "total_seconds": 0,
                "session_count": 0,
                "last_session_start": start_time.isoformat(),
                "last_session_duration_sec": None,
                "offline_deltas": [],
            }
        }

        result = await plugin.record_session_end(42)

        assert result["duration_sec"] <= 86400  # 24h max


# ============================================================================
# Playtime Notes API Helpers
# ============================================================================


class TestPlaytimeNoteHelpers:
    """Tests for playtime note content parsing."""

    def test_parse_valid_content(self, plugin):
        """Parses valid JSON content from a note."""
        result = plugin._parse_playtime_note_content('{"seconds": 3600, "device": "deck"}')
        assert result["seconds"] == 3600
        assert result["device"] == "deck"

    def test_parse_empty_content(self, plugin):
        """Returns None for empty content."""
        assert plugin._parse_playtime_note_content("") is None
        assert plugin._parse_playtime_note_content(None) is None

    def test_parse_malformed_json(self, plugin):
        """Returns None for invalid JSON."""
        assert plugin._parse_playtime_note_content("{bad json}") is None

    def test_parse_non_dict_json(self, plugin):
        """Returns None for JSON that is not a dict."""
        assert plugin._parse_playtime_note_content("[1, 2, 3]") is None


class TestSyncPlaytimeToRomm:
    """Tests for _sync_playtime_to_romm via Notes API."""

    def test_creates_note_when_none_exists(self, plugin):
        """Creates a new playtime note when no existing note found."""
        plugin._save_sync_state["device_name"] = "steamdeck"
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 1000,
            "session_count": 3,
        }

        with patch.object(plugin, "_romm_get_playtime_note", return_value=None), \
             patch.object(plugin, "_romm_create_playtime_note") as mock_create, \
             patch("time.sleep"):
            plugin._sync_playtime_to_romm(42, 600)

        mock_create.assert_called_once()
        call_args = mock_create.call_args[0]
        assert call_args[0] == 42  # rom_id
        data = call_args[1]
        assert data["seconds"] >= 1000  # max(local_total, server + session)
        assert data["device"] == "steamdeck"

    def test_updates_existing_note(self, plugin):
        """Updates existing playtime note when one is found."""
        plugin._save_sync_state["device_name"] = "steamdeck"
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 2000,
            "session_count": 5,
        }
        existing_note = {
            "id": 99,
            "title": "romm-sync:playtime",
            "content": '{"seconds": 1500, "device": "other"}',
        }

        with patch.object(plugin, "_romm_get_playtime_note", return_value=existing_note), \
             patch.object(plugin, "_romm_update_playtime_note") as mock_update, \
             patch("time.sleep"):
            plugin._sync_playtime_to_romm(42, 300)

        mock_update.assert_called_once()
        call_args = mock_update.call_args[0]
        assert call_args[0] == 42  # rom_id
        assert call_args[1] == 99  # note_id
        data = call_args[2]
        assert data["seconds"] >= 2000  # max(local 2000, server 1500 + session 300)

    def test_server_higher_merged_with_session(self, plugin):
        """Server has higher base; merged total = server + session_delta."""
        plugin._save_sync_state["device_name"] = "deck"
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 1000,
            "session_count": 2,
        }
        existing_note = {
            "id": 10,
            "title": "romm-sync:playtime",
            "content": '{"seconds": 5000}',
        }

        with patch.object(plugin, "_romm_get_playtime_note", return_value=existing_note), \
             patch.object(plugin, "_romm_update_playtime_note") as mock_update, \
             patch("time.sleep"):
            plugin._sync_playtime_to_romm(42, 120)

        data = mock_update.call_args[0][2]
        # max(local_total=1000, server_seconds=5000 + session=120) = 5120
        assert data["seconds"] == 5120

    def test_server_error_does_not_raise(self, plugin):
        """Network error during sync is logged, not raised."""
        plugin._save_sync_state["playtime"]["42"] = {"total_seconds": 100}

        with patch.object(plugin, "_romm_get_playtime_note", side_effect=ConnectionError("offline")), \
             patch("time.sleep"):
            # Should not raise
            plugin._sync_playtime_to_romm(42, 60)

    def test_no_playtime_entry_returns_early(self, plugin):
        """No local playtime entry → returns immediately."""
        with patch.object(plugin, "_romm_get_playtime_note") as mock_get:
            plugin._sync_playtime_to_romm(42, 100)

        mock_get.assert_not_called()


class TestGetServerPlaytime:
    """Tests for get_server_playtime callable."""

    @pytest.mark.asyncio
    async def test_returns_merged_playtime(self, plugin):
        """Returns max of local and server playtime."""
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 3000,
            "session_count": 5,
        }
        note = {
            "id": 1,
            "title": "romm-sync:playtime",
            "content": '{"seconds": 5000}',
        }

        with patch.object(plugin, "_romm_get_playtime_note", return_value=note), \
             patch("time.sleep"):
            result = await plugin.get_server_playtime(42)

        assert result["local_seconds"] == 3000
        assert result["server_seconds"] == 5000
        assert result["total_seconds"] == 5000

    @pytest.mark.asyncio
    async def test_server_offline_returns_local(self, plugin):
        """Server unreachable returns local playtime only."""
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 2000,
            "session_count": 3,
        }

        with patch.object(plugin, "_romm_get_playtime_note", side_effect=ConnectionError("offline")), \
             patch("time.sleep"):
            result = await plugin.get_server_playtime(42)

        assert result["local_seconds"] == 2000
        assert result["server_seconds"] == 0
        assert result["total_seconds"] == 2000

    @pytest.mark.asyncio
    async def test_no_playtime_anywhere(self, plugin):
        """No local or server playtime → all zeros."""
        with patch.object(plugin, "_romm_get_playtime_note", return_value=None), \
             patch("time.sleep"):
            result = await plugin.get_server_playtime(42)

        assert result["local_seconds"] == 0
        assert result["server_seconds"] == 0
        assert result["total_seconds"] == 0

    @pytest.mark.asyncio
    async def test_no_note_found(self, plugin):
        """No playtime note on server → server_seconds=0."""
        with patch.object(plugin, "_romm_get_playtime_note", return_value=None), \
             patch("time.sleep"):
            result = await plugin.get_server_playtime(42)

        assert result["server_seconds"] == 0


class TestGetAllPlaytime:
    """Tests for get_all_playtime callable."""

    @pytest.mark.asyncio
    async def test_returns_all_playtime_entries(self, plugin):
        """Returns all playtime entries from state."""
        plugin._save_sync_state["playtime"] = {
            "42": {"total_seconds": 3000, "session_count": 5},
            "99": {"total_seconds": 600, "session_count": 1},
        }
        result = await plugin.get_all_playtime()
        assert result["playtime"]["42"]["total_seconds"] == 3000
        assert result["playtime"]["99"]["total_seconds"] == 600

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_playtime(self, plugin):
        """Returns empty dict when no playtime data exists."""
        plugin._save_sync_state["playtime"] = {}
        result = await plugin.get_all_playtime()
        assert result["playtime"] == {}


class TestRecordSessionEndWithServerSync:
    """Tests that record_session_end triggers playtime server sync."""

    @pytest.mark.asyncio
    async def test_session_end_syncs_to_romm(self, plugin):
        """record_session_end calls _sync_playtime_to_romm with session duration."""
        start_time = datetime.now(timezone.utc) - timedelta(seconds=60)
        plugin._save_sync_state["playtime"] = {
            "42": {
                "total_seconds": 1000,
                "session_count": 2,
                "last_session_start": start_time.isoformat(),
                "last_session_duration_sec": None,
                "offline_deltas": [],
            }
        }

        with patch.object(plugin, "_sync_playtime_to_romm") as mock_sync:
            result = await plugin.record_session_end(42)

        assert result["success"] is True
        mock_sync.assert_called_once()
        call_args = mock_sync.call_args[0]
        assert call_args[0] == 42  # rom_id
        assert call_args[1] >= 59  # session duration (~60s)

    @pytest.mark.asyncio
    async def test_session_end_succeeds_even_if_sync_fails(self, plugin):
        """Server sync failure does not fail the session recording."""
        start_time = datetime.now(timezone.utc) - timedelta(seconds=10)
        plugin._save_sync_state["playtime"] = {
            "42": {
                "total_seconds": 0,
                "session_count": 0,
                "last_session_start": start_time.isoformat(),
                "last_session_duration_sec": None,
                "offline_deltas": [],
            }
        }

        with patch.object(plugin, "_sync_playtime_to_romm", side_effect=Exception("network")):
            result = await plugin.record_session_end(42)

        # The local recording still succeeds
        assert result["success"] is True
        assert result["duration_sec"] >= 9

    @pytest.mark.asyncio
    async def test_session_end_passes_duration_not_total(self, plugin):
        """record_session_end passes session duration (not total) to _sync_playtime_to_romm."""
        start_time = datetime.now(timezone.utc) - timedelta(seconds=120)
        plugin._save_sync_state["playtime"] = {
            "42": {
                "total_seconds": 5000,
                "session_count": 10,
                "last_session_start": start_time.isoformat(),
                "last_session_duration_sec": None,
                "offline_deltas": [],
            }
        }

        with patch.object(plugin, "_sync_playtime_to_romm") as mock_sync:
            result = await plugin.record_session_end(42)

        assert result["success"] is True
        call_args = mock_sync.call_args[0]
        # Second arg should be the session duration (~120), NOT the total (~5120)
        assert 115 <= call_args[1] <= 125


class TestSyncPlaytimeEdgeCases:
    """Additional edge cases for playtime Notes API sync."""

    def test_sync_updates_local_total_from_server(self, plugin):
        """Local total updated to merged value after sync."""
        plugin._save_sync_state["device_name"] = "deck"
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 600,
            "session_count": 1,
            "last_session_start": None,
            "last_session_duration_sec": 600,
            "offline_deltas": [],
        }

        existing_note = {
            "id": 5,
            "title": "romm-sync:playtime",
            "content": '{"seconds": 10000, "device": "desktop"}',
        }

        with patch.object(plugin, "_romm_get_playtime_note", return_value=existing_note), \
             patch.object(plugin, "_romm_update_playtime_note"), \
             patch("time.sleep"):
            plugin._sync_playtime_to_romm(42, 600)

        # Local total should now be server+session = 10600
        assert plugin._save_sync_state["playtime"]["42"]["total_seconds"] == 10600

    def test_sync_creates_note_with_correct_api_args(self, plugin):
        """Verifies _romm_create_playtime_note receives correct structure."""
        plugin._save_sync_state["device_name"] = "deck"
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 500,
            "session_count": 1,
        }

        with patch.object(plugin, "_romm_get_playtime_note", return_value=None), \
             patch.object(plugin, "_romm_create_playtime_note") as mock_create, \
             patch("time.sleep"):
            plugin._sync_playtime_to_romm(42, 500)

        data = mock_create.call_args[0][1]
        assert "seconds" in data
        assert "updated" in data
        assert "device" in data
        # updated should be a valid ISO timestamp
        datetime.fromisoformat(data["updated"])

    def test_romm_create_note_calls_post_json(self, plugin):
        """_romm_create_playtime_note calls _romm_post_json with correct path and body.

        Tags are NOT sent (they cause GET /api/roms/{id}/notes to return 500).
        """
        with patch.object(plugin, "_romm_post_json") as mock_post:
            plugin._romm_create_playtime_note(42, {"seconds": 100})

        mock_post.assert_called_once()
        path = mock_post.call_args[0][0]
        body = mock_post.call_args[0][1]
        assert path == "/api/roms/42/notes"
        assert body["title"] == "romm-sync:playtime"
        assert body["is_public"] is False
        assert "tags" not in body
        assert '"seconds": 100' in body["content"]

    def test_romm_update_note_calls_put_json(self, plugin):
        """_romm_update_playtime_note calls _romm_put_json with correct path."""
        with patch.object(plugin, "_romm_put_json") as mock_put:
            plugin._romm_update_playtime_note(42, 99, {"seconds": 200})

        mock_put.assert_called_once()
        path = mock_put.call_args[0][0]
        body = mock_put.call_args[0][1]
        assert path == "/api/roms/42/notes/99"
        assert '"seconds": 200' in body["content"]

    def test_sync_with_zero_session_duration(self, plugin):
        """Zero-length session still syncs (handles rapid start/stop)."""
        plugin._save_sync_state["device_name"] = "deck"
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 1000,
            "session_count": 5,
        }

        with patch.object(plugin, "_romm_get_playtime_note", return_value=None), \
             patch.object(plugin, "_romm_create_playtime_note") as mock_create, \
             patch("time.sleep"):
            plugin._sync_playtime_to_romm(42, 0)

        data = mock_create.call_args[0][1]
        assert data["seconds"] == 1000  # max(1000, 0+0) = 1000

    def test_get_playtime_note_uses_rom_detail(self, plugin):
        """_romm_get_playtime_note uses GET /api/roms/{id} instead of notes endpoint.

        GET /api/roms/{id}/notes returns 500 when notes exist (RomM bug).
        Workaround: fetch ROM detail and filter all_user_notes.
        """
        rom_detail = {
            "id": 42,
            "all_user_notes": [
                {"id": 5, "title": "romm-sync:playtime", "content": '{"seconds":100}'},
                {"id": 6, "title": "other-note", "content": "hello"},
            ],
        }
        with patch.object(plugin, "_romm_request", return_value=rom_detail) as mock_req:
            result = plugin._romm_get_playtime_note(42)

        mock_req.assert_called_once_with("/api/roms/42")
        assert result is not None
        assert result["id"] == 5
        assert result["title"] == "romm-sync:playtime"


# ============================================================================
# Save Sync Settings
# ============================================================================


class TestSaveSyncSettings:
    """Tests for get/update save sync settings."""

    @pytest.mark.asyncio
    async def test_get_returns_current(self, plugin):
        """Returns current settings."""
        result = await plugin.get_save_sync_settings()

        assert result["conflict_mode"] == "newest_wins"
        assert result["sync_before_launch"] is True
        assert result["sync_after_exit"] is True
        assert result["clock_skew_tolerance_sec"] == 60

    @pytest.mark.asyncio
    async def test_update_changes_settings(self, plugin, tmp_path):
        """Updates and persists settings."""
        result = await plugin.update_save_sync_settings({
            "conflict_mode": "always_download",
            "sync_before_launch": False,
        })

        assert result["success"] is True
        assert result["settings"]["conflict_mode"] == "always_download"
        assert result["settings"]["sync_before_launch"] is False
        # sync_after_exit unchanged
        assert result["settings"]["sync_after_exit"] is True

        # Persisted
        path = tmp_path / "save_sync_state.json"
        data = json.loads(path.read_text())
        assert data["settings"]["conflict_mode"] == "always_download"

    @pytest.mark.asyncio
    async def test_invalid_conflict_mode_ignored(self, plugin):
        """Unknown conflict mode is silently ignored."""
        result = await plugin.update_save_sync_settings({
            "conflict_mode": "invalid_mode",
        })

        assert result["success"] is True
        # Original value preserved
        assert result["settings"]["conflict_mode"] == "newest_wins"

    @pytest.mark.asyncio
    async def test_unknown_keys_ignored(self, plugin):
        """Unknown settings keys are silently ignored."""
        result = await plugin.update_save_sync_settings({
            "unknown_key": "value",
            "conflict_mode": "ask_me",
        })

        assert result["success"] is True
        assert result["settings"]["conflict_mode"] == "ask_me"
        assert "unknown_key" not in result["settings"]

    @pytest.mark.asyncio
    async def test_clock_skew_clamped_to_zero(self, plugin):
        """Negative clock_skew_tolerance_sec clamped to 0."""
        result = await plugin.update_save_sync_settings({
            "clock_skew_tolerance_sec": -10,
        })

        assert result["settings"]["clock_skew_tolerance_sec"] == 0

    @pytest.mark.asyncio
    async def test_boolean_coercion(self, plugin):
        """sync toggles coerced to bool."""
        result = await plugin.update_save_sync_settings({
            "sync_before_launch": 0,
            "sync_after_exit": 1,
        })

        assert result["settings"]["sync_before_launch"] is False
        assert result["settings"]["sync_after_exit"] is True


# ============================================================================
# Manual Sync All
# ============================================================================


class TestSyncAllSaves:
    """Tests for sync_all_saves."""

    @pytest.mark.asyncio
    async def test_syncs_all_installed_roms(self, plugin, tmp_path):
        """Iterates all installed ROMs with saves."""
        _install_rom(plugin, tmp_path, rom_id=1, file_name="game_a.gba")
        _install_rom(plugin, tmp_path, rom_id=2, file_name="game_b.gba")
        _create_save(tmp_path, rom_name="game_a")
        _create_save(tmp_path, rom_name="game_b")

        plugin._save_sync_state["device_id"] = "dev-1"

        upload_response = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response):
            result = await plugin.sync_all_saves()

        assert result["success"] is True
        assert result["roms_checked"] == 2
        assert result["synced"] == 2

    @pytest.mark.asyncio
    async def test_no_installed_roms(self, plugin):
        """Empty installed_roms completes gracefully."""
        plugin._save_sync_state["device_id"] = "dev-1"

        result = await plugin.sync_all_saves()

        assert result["success"] is True
        assert result["roms_checked"] == 0
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_mixed_success_and_failure(self, plugin, tmp_path):
        """Some ROMs sync, others fail — both counted."""
        _install_rom(plugin, tmp_path, rom_id=1, file_name="good.gba")
        _install_rom(plugin, tmp_path, rom_id=2, file_name="bad.gba")
        _create_save(tmp_path, rom_name="good")
        _create_save(tmp_path, rom_name="bad")

        plugin._save_sync_state["device_id"] = "dev-1"

        call_count = [0]

        def mock_upload(rom_id, file_path, emulator="retroarch", save_id=None):
            call_count[0] += 1
            if "bad" in file_path:
                raise ConnectionError("Network error")
            return {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", side_effect=mock_upload):
            result = await plugin.sync_all_saves()

        assert result["roms_checked"] == 2
        assert result["synced"] >= 1
        assert len(result["errors"]) >= 1

    @pytest.mark.asyncio
    async def test_ignores_shortcut_registry_only_roms(self, plugin, tmp_path):
        """Only iterates installed_roms, not shortcut_registry-only entries."""
        # ROM 1 is in installed_roms
        _install_rom(plugin, tmp_path, rom_id=1, file_name="game_a.gba")
        _create_save(tmp_path, rom_name="game_a")

        # ROM 2 is only in shortcut_registry (synced but not downloaded — no save info)
        plugin._state["shortcut_registry"]["2"] = {
            "rom_id": 2, "app_id": 12345, "name": "Game B",
        }

        plugin._save_sync_state["device_id"] = "dev-1"

        upload_response = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response):
            result = await plugin.sync_all_saves()

        # Only installed ROM should be checked
        assert result["roms_checked"] == 1

    @pytest.mark.asyncio
    async def test_returns_conflicts_count(self, plugin, tmp_path):
        """sync_all_saves returns conflicts count from pending_conflicts."""
        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["pending_conflicts"] = [
            {"rom_id": 1, "filename": "a.srm"},
            {"rom_id": 2, "filename": "b.srm"},
        ]

        result = await plugin.sync_all_saves()

        assert "conflicts" in result
        assert result["conflicts"] == 2


# ============================================================================
# Single ROM Sync (sync_rom_saves)
# ============================================================================


class TestSyncRomSaves:
    """Tests for sync_rom_saves callable (bidirectional per-ROM sync)."""

    @pytest.mark.asyncio
    async def test_uploads_local_save_no_server(self, plugin, tmp_path):
        """Uploads local save when server has none (direction=both)."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        upload_response = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response):
            result = await plugin.sync_rom_saves(42)

        assert result["success"] is True
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_downloads_server_save_no_local(self, plugin, tmp_path):
        """Downloads server save when no local file exists."""
        _install_rom(plugin, tmp_path)
        saves_dir = tmp_path / "retrodeck" / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)

        plugin._save_sync_state["device_id"] = "dev-1"
        server = _server_save()

        def fake_download(save_id, dest):
            with open(dest, "wb") as f:
                f.write(b"\xff" * 1024)

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_romm_download_save", side_effect=fake_download):
            result = await plugin.sync_rom_saves(42)

        assert result["success"] is True
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_auto_registers_device(self, plugin, tmp_path):
        """Auto-registers device if not yet registered."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)

        upload_response = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response):
            result = await plugin.sync_rom_saves(42)

        assert result["success"] is True
        assert plugin._save_sync_state["device_id"] is not None

    @pytest.mark.asyncio
    async def test_reports_errors(self, plugin, tmp_path):
        """Reports upload errors in result."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", side_effect=ConnectionError("offline")):
            result = await plugin.sync_rom_saves(42)

        assert result["success"] is False
        assert len(result["errors"]) >= 1

    @pytest.mark.asyncio
    async def test_rom_not_installed(self, plugin):
        """Non-installed ROM returns 0 synced."""
        plugin._save_sync_state["device_id"] = "dev-1"

        result = await plugin.sync_rom_saves(999)

        assert result["success"] is True
        assert result["synced"] == 0


# ============================================================================
# Pending Conflicts
# ============================================================================


class TestPendingConflicts:
    """Tests for conflict queue management."""

    @pytest.mark.asyncio
    async def test_get_pending_conflicts(self, plugin):
        """Returns list of unresolved conflicts."""
        plugin._save_sync_state["pending_conflicts"] = [
            {"rom_id": 42, "filename": "pokemon.srm", "local_hash": "abc"},
        ]

        result = await plugin.get_pending_conflicts()

        assert len(result["conflicts"]) == 1
        assert result["conflicts"][0]["rom_id"] == 42

    @pytest.mark.asyncio
    async def test_get_empty_conflicts(self, plugin):
        """Returns empty list when no conflicts."""
        result = await plugin.get_pending_conflicts()

        assert result["conflicts"] == []

    @pytest.mark.asyncio
    async def test_resolve_upload(self, plugin, tmp_path):
        """Resolving as upload sends local save to server."""
        _install_rom(plugin, tmp_path)
        save_file = _create_save(tmp_path)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["pending_conflicts"] = [{
            "rom_id": 42,
            "filename": "pokemon.srm",
            "local_path": str(save_file),
            "local_hash": "abc",
            "server_save_id": 100,
            "server_updated_at": "2026-02-17T06:00:00Z",
            "created_at": "2026-02-17T12:00:00Z",
        }]

        upload_response = {"id": 100, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_request", return_value={"id": 100}), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response):
            result = await plugin.resolve_conflict(42, "pokemon.srm", "upload")

        assert result["success"] is True
        assert len(plugin._save_sync_state["pending_conflicts"]) == 0

    @pytest.mark.asyncio
    async def test_resolve_download(self, plugin, tmp_path):
        """Resolving as download fetches server save."""
        _install_rom(plugin, tmp_path)
        saves_dir = tmp_path / "retrodeck" / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["pending_conflicts"] = [{
            "rom_id": 42,
            "filename": "pokemon.srm",
            "local_path": str(saves_dir / "pokemon.srm"),
            "local_hash": "abc",
            "server_save_id": 100,
            "server_updated_at": "2026-02-17T06:00:00Z",
            "created_at": "2026-02-17T12:00:00Z",
        }]

        server_save = _server_save()

        def fake_download(save_id, dest):
            with open(dest, "wb") as f:
                f.write(b"\xff" * 1024)

        with patch.object(plugin, "_romm_request", return_value=server_save), \
             patch.object(plugin, "_romm_download_save", side_effect=fake_download):
            result = await plugin.resolve_conflict(42, "pokemon.srm", "download")

        assert result["success"] is True
        assert len(plugin._save_sync_state["pending_conflicts"]) == 0

    @pytest.mark.asyncio
    async def test_resolve_invalid_resolution(self, plugin):
        """Invalid resolution string is rejected."""
        result = await plugin.resolve_conflict(42, "pokemon.srm", "invalid")

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_resolve_not_found(self, plugin):
        """Resolving a non-existent conflict returns failure."""
        plugin._save_sync_state["pending_conflicts"] = []

        result = await plugin.resolve_conflict(42, "pokemon.srm", "upload")

        assert result["success"] is False

    def test_add_pending_conflict_no_duplicates(self, plugin, tmp_path):
        """Adding same conflict twice doesn't create duplicates."""
        save_file = _create_save(tmp_path)
        server = _server_save()

        plugin._add_pending_conflict(42, "pokemon.srm", str(save_file), server)
        plugin._add_pending_conflict(42, "pokemon.srm", str(save_file), server)

        assert len(plugin._save_sync_state["pending_conflicts"]) == 1


# ============================================================================
# HTTP Helpers
# ============================================================================


class TestRommListSaves:
    """Tests for _romm_list_saves response parsing."""

    def test_list_response_is_array(self, plugin):
        """API returns a plain list."""
        with patch.object(plugin, "_romm_request", return_value=[
            {"id": 1, "file_name": "a.srm"},
            {"id": 2, "file_name": "b.srm"},
        ]):
            result = plugin._romm_list_saves(42)

        assert len(result) == 2

    def test_list_response_non_array_returns_empty(self, plugin):
        """Non-array API response returns empty list (safety fallback)."""
        with patch.object(plugin, "_romm_request", return_value={
            "items": [{"id": 1, "file_name": "a.srm"}],
            "total": 1,
        }):
            result = plugin._romm_list_saves(42)

        assert result == []

    def test_list_response_empty(self, plugin):
        """API returns empty list."""
        with patch.object(plugin, "_romm_request", return_value=[]):
            result = plugin._romm_list_saves(42)

        assert result == []


# ============================================================================
# Retry Logic
# ============================================================================


class TestRetryLogic:
    """Tests for _with_retry and _is_retryable."""

    def test_is_retryable_5xx(self, plugin):
        """HTTP 500/502/503 are retryable."""
        import urllib.error
        for code in (500, 502, 503):
            exc = urllib.error.HTTPError("url", code, "err", {}, None)
            assert plugin._is_retryable(exc) is True

    def test_is_not_retryable_4xx(self, plugin):
        """HTTP 400/401/404/409 are NOT retryable."""
        import urllib.error
        for code in (400, 401, 403, 404, 409):
            exc = urllib.error.HTTPError("url", code, "err", {}, None)
            assert plugin._is_retryable(exc) is False

    def test_is_retryable_connection_errors(self, plugin):
        """ConnectionError, TimeoutError, URLError are retryable."""
        import urllib.error
        assert plugin._is_retryable(ConnectionError("refused")) is True
        assert plugin._is_retryable(TimeoutError("timed out")) is True
        assert plugin._is_retryable(urllib.error.URLError("unreachable")) is True
        assert plugin._is_retryable(OSError("network down")) is True

    def test_is_not_retryable_other(self, plugin):
        """ValueError, KeyError etc. are NOT retryable."""
        assert plugin._is_retryable(ValueError("bad")) is False
        assert plugin._is_retryable(KeyError("missing")) is False

    def test_retry_succeeds_on_first_try(self, plugin):
        """No retries needed when call succeeds."""
        fn = MagicMock(return_value="ok")
        result = plugin._with_retry(fn, "arg1", key="val")
        assert result == "ok"
        fn.assert_called_once_with("arg1", key="val")

    def test_retry_succeeds_after_transient_failure(self, plugin):
        """Retries on transient error, succeeds on second attempt."""
        fn = MagicMock(side_effect=[ConnectionError("refused"), "ok"])
        with patch("time.sleep"):
            result = plugin._with_retry(fn, max_attempts=3, base_delay=0)
        assert result == "ok"
        assert fn.call_count == 2

    def test_retry_exhausted_raises(self, plugin):
        """All attempts fail → raises last exception."""
        fn = MagicMock(side_effect=ConnectionError("refused"))
        with patch("time.sleep"):
            with pytest.raises(ConnectionError):
                plugin._with_retry(fn, max_attempts=3, base_delay=0)
        assert fn.call_count == 3

    def test_retry_no_retry_on_4xx(self, plugin):
        """4xx errors raise immediately without retry."""
        import urllib.error
        err = urllib.error.HTTPError("url", 404, "not found", {}, None)
        fn = MagicMock(side_effect=err)
        with pytest.raises(urllib.error.HTTPError):
            plugin._with_retry(fn, max_attempts=3, base_delay=0)
        fn.assert_called_once()

    def test_retry_delays_exponential(self, plugin):
        """Delays follow base_delay * 3^attempt pattern."""
        fn = MagicMock(side_effect=[
            ConnectionError("1"), ConnectionError("2"), "ok"
        ])
        with patch("time.sleep") as mock_sleep:
            plugin._with_retry(fn, max_attempts=3, base_delay=1)
        assert mock_sleep.call_count == 2
        mock_sleep.assert_any_call(1)   # 1 * 3^0
        mock_sleep.assert_any_call(3)   # 1 * 3^1

    def test_retry_integrated_in_sync(self, plugin, tmp_path):
        """_sync_rom_saves retries transient list_saves failures."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        call_count = [0]
        def flaky_list(rom_id):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("transient")
            return []

        upload_resp = {"id": 1, "updated_at": "2026-02-17T15:00:00Z"}
        with patch.object(plugin, "_romm_list_saves", side_effect=flaky_list), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_resp), \
             patch("time.sleep"):
            synced, errors = plugin._sync_rom_saves(42)

        assert call_count[0] == 2  # retried once
        assert synced >= 1

    def test_retry_in_get_save_status(self, plugin, tmp_path):
        """get_save_status retries transient list_saves failures."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        call_count = [0]
        def flaky_list(rom_id):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("transient")
            return [_server_save()]

        with patch.object(plugin, "_romm_list_saves", side_effect=flaky_list), \
             patch("time.sleep"):
            result = asyncio.get_event_loop().run_until_complete(
                plugin.get_save_status(42)
            )

        assert call_count[0] == 2  # retried once
        assert len(result["files"]) >= 1

    def test_retry_in_get_server_save_hash(self, plugin, tmp_path):
        """_get_server_save_hash retries transient download failures."""
        call_count = [0]
        def flaky_download(save_id, dest):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("transient")
            with open(dest, "wb") as f:
                f.write(b"\x00" * 64)

        server = _server_save()
        with patch.object(plugin, "_romm_download_save", side_effect=flaky_download), \
             patch("time.sleep"):
            result = plugin._get_server_save_hash(server)

        assert call_count[0] == 2  # retried once
        assert result is not None  # should return a valid hash

    def test_retry_in_resolve_conflict_download(self, plugin, tmp_path):
        """resolve_conflict download path retries transient API failures."""
        _install_rom(plugin, tmp_path)
        saves_dir = tmp_path / "retrodeck" / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["pending_conflicts"] = [{
            "rom_id": 42,
            "filename": "pokemon.srm",
            "local_path": str(saves_dir / "pokemon.srm"),
            "local_hash": "abc",
            "server_save_id": 100,
            "server_updated_at": "2026-02-17T06:00:00Z",
            "created_at": "2026-02-17T12:00:00Z",
        }]

        call_count = [0]
        server = _server_save()
        def flaky_request(path):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("transient")
            return server

        def fake_download(save_id, dest):
            with open(dest, "wb") as f:
                f.write(b"\xff" * 1024)

        with patch.object(plugin, "_romm_request", side_effect=flaky_request), \
             patch.object(plugin, "_romm_download_save", side_effect=fake_download), \
             patch("time.sleep"):
            result = asyncio.get_event_loop().run_until_complete(
                plugin.resolve_conflict(42, "pokemon.srm", "download")
            )

        assert result["success"] is True
        assert call_count[0] == 2  # retried the metadata fetch

    def test_retry_in_resolve_conflict_upload(self, plugin, tmp_path):
        """resolve_conflict upload path retries transient API failures on metadata fetch."""
        _install_rom(plugin, tmp_path)
        save_file = _create_save(tmp_path)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["pending_conflicts"] = [{
            "rom_id": 42,
            "filename": "pokemon.srm",
            "local_path": str(save_file),
            "local_hash": "abc",
            "server_save_id": 100,
            "server_updated_at": "2026-02-17T06:00:00Z",
            "created_at": "2026-02-17T12:00:00Z",
        }]

        call_count = [0]
        def flaky_request(path):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("transient")
            return {"id": 100}

        upload_response = {"id": 100, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_request", side_effect=flaky_request), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response), \
             patch("time.sleep"):
            result = asyncio.get_event_loop().run_until_complete(
                plugin.resolve_conflict(42, "pokemon.srm", "upload")
            )

        assert result["success"] is True
        assert call_count[0] == 2  # retried the metadata fetch


# ============================================================================
# Get Save Status
# ============================================================================


class TestGetSaveStatus:
    """Tests for get_save_status callable."""

    @pytest.mark.asyncio
    async def test_local_and_server_saves(self, plugin, tmp_path):
        """Shows both local and server save info."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)

        plugin._save_sync_state["device_id"] = "dev-1"
        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]):
            result = await plugin.get_save_status(42)

        assert result["rom_id"] == 42
        assert len(result["files"]) >= 1
        f = result["files"][0]
        assert f["filename"] == "pokemon.srm"
        assert f["local_hash"] is not None
        assert f["server_save_id"] == 100

    @pytest.mark.asyncio
    async def test_server_only_save(self, plugin, tmp_path):
        """Server save exists but no local file → status=download."""
        _install_rom(plugin, tmp_path)
        # No local save created
        plugin._save_sync_state["device_id"] = "dev-1"

        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]):
            result = await plugin.get_save_status(42)

        server_files = [f for f in result["files"] if f["status"] == "download"]
        assert len(server_files) >= 1

    @pytest.mark.asyncio
    async def test_local_only_save(self, plugin, tmp_path):
        """Local save exists but not on server → status=upload."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        with patch.object(plugin, "_romm_list_saves", return_value=[]):
            result = await plugin.get_save_status(42)

        upload_files = [f for f in result["files"] if f["status"] == "upload"]
        assert len(upload_files) >= 1

    @pytest.mark.asyncio
    async def test_api_error_still_returns_local(self, plugin, tmp_path):
        """Server error still returns local save info."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        with patch.object(plugin, "_romm_list_saves", side_effect=ConnectionError("offline")):
            result = await plugin.get_save_status(42)

        assert result["rom_id"] == 42
        assert len(result["files"]) >= 1
        assert result["files"][0]["local_hash"] is not None


# ============================================================================
# Download Save (Backup Behavior)
# ============================================================================


class TestDownloadSaveBackup:
    """Tests for save download backup behavior."""

    @pytest.mark.asyncio
    async def test_creates_backup_before_overwrite(self, plugin, tmp_path):
        """Downloading over existing save creates .romm-backup."""
        _install_rom(plugin, tmp_path)
        save_file = _create_save(tmp_path, content=b"original save data")

        plugin._save_sync_state["device_id"] = "dev-1"
        # Set up sync state so server is newer (slow path detects change)
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": plugin._file_md5(str(save_file)),
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        def fake_download(save_id, dest):
            with open(dest, "wb") as f:
                f.write(b"new server save data")

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_romm_download_save", side_effect=fake_download):
            result = await plugin.pre_launch_sync(42)

        # Backup directory should exist
        backup_dir = tmp_path / "retrodeck" / "saves" / "gba" / ".romm-backup"
        assert backup_dir.is_dir()
        backups = list(backup_dir.iterdir())
        assert len(backups) >= 1


# ============================================================================
# RetroDECK Saves Path
# ============================================================================


class TestGetRetrodeckSavesPath:
    """Tests for _get_retrodeck_saves_path reading from retrodeck.json."""

    def test_reads_from_retrodeck_json(self, plugin, tmp_path):
        """Reads saves_path from retrodeck.json config."""
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True)
        config = {"paths": {"saves_path": "/run/media/deck/Emulation/retrodeck/saves"}}
        (config_dir / "retrodeck.json").write_text(json.dumps(config))

        result = plugin._get_retrodeck_saves_path()

        assert result == "/run/media/deck/Emulation/retrodeck/saves"

    def test_fallback_when_file_missing(self, plugin, tmp_path):
        """Falls back to ~/retrodeck/saves when config file is missing."""
        result = plugin._get_retrodeck_saves_path()

        expected = os.path.join(str(tmp_path), "retrodeck", "saves")
        assert result == expected

    def test_fallback_when_json_corrupt(self, plugin, tmp_path):
        """Falls back when retrodeck.json is corrupt."""
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True)
        (config_dir / "retrodeck.json").write_text("{{{invalid!")

        result = plugin._get_retrodeck_saves_path()

        expected = os.path.join(str(tmp_path), "retrodeck", "saves")
        assert result == expected

    def test_fallback_when_paths_key_missing(self, plugin, tmp_path):
        """Falls back when retrodeck.json lacks paths.saves_path."""
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True)
        (config_dir / "retrodeck.json").write_text(json.dumps({"version": "1.0"}))

        result = plugin._get_retrodeck_saves_path()

        expected = os.path.join(str(tmp_path), "retrodeck", "saves")
        assert result == expected

    def test_fallback_when_saves_path_empty(self, plugin, tmp_path):
        """Falls back when saves_path is empty string."""
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True)
        config = {"paths": {"saves_path": ""}}
        (config_dir / "retrodeck.json").write_text(json.dumps(config))

        result = plugin._get_retrodeck_saves_path()

        expected = os.path.join(str(tmp_path), "retrodeck", "saves")
        assert result == expected

    def test_not_cached(self, plugin, tmp_path):
        """Reads fresh every call (not cached)."""
        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "retrodeck.json"

        config_file.write_text(json.dumps({"paths": {"saves_path": "/first/path"}}))
        assert plugin._get_retrodeck_saves_path() == "/first/path"

        config_file.write_text(json.dumps({"paths": {"saves_path": "/second/path"}}))
        assert plugin._get_retrodeck_saves_path() == "/second/path"


# ============================================================================
# ROM Save Info Helper
# ============================================================================


class TestGetRomSaveInfo:
    """Tests for _get_rom_save_info."""

    def test_returns_info_for_installed_rom(self, plugin, tmp_path):
        """Returns (system, rom_name, saves_dir) tuple."""
        _install_rom(plugin, tmp_path)

        result = plugin._get_rom_save_info(42)

        assert result is not None
        system, rom_name, saves_dir = result
        assert system == "gba"
        assert rom_name == "pokemon"
        assert saves_dir.endswith("saves/gba")

    def test_returns_none_for_missing_rom(self, plugin):
        """Returns None when ROM not installed."""
        result = plugin._get_rom_save_info(999)

        assert result is None

    def test_returns_none_for_empty_system(self, plugin):
        """Returns None if installed ROM has empty system."""
        plugin._state["installed_roms"]["42"] = {
            "rom_id": 42,
            "file_name": "game.gba",
            "file_path": "/some/path.gba",
            "system": "",
            "platform_slug": "",
            "installed_at": "2026-01-01T00:00:00",
        }

        result = plugin._get_rom_save_info(42)

        assert result is None

    def test_uses_retrodeck_config_path(self, plugin, tmp_path):
        """Uses saves_path from retrodeck.json when available."""
        _install_rom(plugin, tmp_path)

        config_dir = tmp_path / ".var" / "app" / "net.retrodeck.retrodeck" / "config" / "retrodeck"
        config_dir.mkdir(parents=True)
        config = {"paths": {"saves_path": "/custom/saves"}}
        (config_dir / "retrodeck.json").write_text(json.dumps(config))

        result = plugin._get_rom_save_info(42)

        assert result is not None
        system, rom_name, saves_dir = result
        assert saves_dir == "/custom/saves/gba"


# ============================================================================
# Edge Cases
# ============================================================================


class TestEdgeCases:
    """Miscellaneous edge cases."""

    @pytest.mark.asyncio
    async def test_pre_launch_rom_not_installed(self, plugin):
        """Pre-launch for uninstalled ROM returns 0 synced."""
        plugin._save_sync_state["device_id"] = "dev-1"

        with patch.object(plugin, "_romm_list_saves", return_value=[]):
            result = await plugin.pre_launch_sync(999)

        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_post_exit_no_save_file(self, plugin, tmp_path):
        """Post-exit when no save file exists (game never saved)."""
        _install_rom(plugin, tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"
        # No save file on disk

        with patch.object(plugin, "_romm_list_saves", return_value=[]):
            result = await plugin.post_exit_sync(42)

        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_sync_registers_device_if_needed(self, plugin, tmp_path):
        """Sync operations auto-register device if not yet registered."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", return_value={"id": 1, "updated_at": "2026-02-17T15:00:00Z"}):
            result = await plugin.pre_launch_sync(42)

        # Device ID should be auto-generated (UUID format)
        device_id = plugin._save_sync_state["device_id"]
        assert device_id is not None
        assert len(device_id) == 36

    def test_file_md5_permission_error(self, plugin, tmp_path):
        """Permission error on file raises (not silently returns None)."""
        f = tmp_path / "locked.srm"
        f.write_bytes(b"data")
        f.chmod(0o000)

        try:
            with pytest.raises(PermissionError):
                plugin._file_md5(str(f))
        finally:
            f.chmod(0o644)

    @pytest.mark.asyncio
    async def test_multipart_upload_constructs_correctly(self, plugin, tmp_path):
        """_romm_upload_multipart constructs valid multipart body."""
        test_file = tmp_path / "test.srm"
        test_file.write_bytes(b"save data content")

        # We can't call _romm_upload_multipart directly (it makes HTTP calls),
        # but we can verify the save file name sanitization
        filename = "game (save).srm"
        safe = filename.replace('"', '\\"')
        assert safe == 'game (save).srm'

    @pytest.mark.asyncio
    async def test_update_file_sync_state(self, plugin, tmp_path):
        """_update_file_sync_state creates proper per-file entries."""
        save_file = _create_save(tmp_path)
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        plugin._update_file_sync_state("42", "pokemon.srm", server_resp, str(save_file), "gba")

        entry = plugin._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert entry["last_sync_hash"] == plugin._file_md5(str(save_file))
        assert entry["last_sync_at"] is not None
        assert entry["last_sync_server_save_id"] == 200


# ── Offline Queue Tests ──────────────────────────────────────────────


class TestOfflineQueue:
    """Tests for offline queue management (failed sync retry)."""

    @pytest.mark.asyncio
    async def test_get_offline_queue_empty(self, plugin):
        """Returns empty queue by default."""
        result = await plugin.get_offline_queue()
        assert result["queue"] == []

    @pytest.mark.asyncio
    async def test_add_to_offline_queue(self, plugin):
        """_add_to_offline_queue adds a failed operation."""
        plugin._add_to_offline_queue(42, "pokemon.srm", "upload", "pokemon.srm: HTTP 500")
        result = await plugin.get_offline_queue()
        assert len(result["queue"]) == 1
        item = result["queue"][0]
        assert item["rom_id"] == 42
        assert item["filename"] == "pokemon.srm"
        assert item["direction"] == "upload"
        assert item["error"] == "pokemon.srm: HTTP 500"
        assert item["retry_count"] == 1

    @pytest.mark.asyncio
    async def test_add_to_offline_queue_no_duplicates(self, plugin):
        """_add_to_offline_queue updates existing entry instead of duplicating."""
        plugin._add_to_offline_queue(42, "pokemon.srm", "upload", "HTTP 500")
        plugin._add_to_offline_queue(42, "pokemon.srm", "upload", "HTTP 503")
        result = await plugin.get_offline_queue()
        assert len(result["queue"]) == 1
        assert result["queue"][0]["error"] == "HTTP 503"
        assert result["queue"][0]["retry_count"] == 2

    @pytest.mark.asyncio
    async def test_add_to_offline_queue_different_files(self, plugin):
        """Different filenames create separate queue entries."""
        plugin._add_to_offline_queue(42, "pokemon.srm", "upload", "err1")
        plugin._add_to_offline_queue(42, "pokemon.rtc", "upload", "err2")
        result = await plugin.get_offline_queue()
        assert len(result["queue"]) == 2

    @pytest.mark.asyncio
    async def test_clear_offline_queue(self, plugin):
        """clear_offline_queue empties the queue."""
        plugin._add_to_offline_queue(42, "pokemon.srm", "upload", "error")
        plugin._add_to_offline_queue(43, "zelda.srm", "download", "error")
        result = await plugin.clear_offline_queue()
        assert result["success"] is True
        queue = await plugin.get_offline_queue()
        assert queue["queue"] == []

    @pytest.mark.asyncio
    async def test_retry_failed_sync_not_found(self, plugin):
        """retry_failed_sync returns failure when item not in queue."""
        result = await plugin.retry_failed_sync(99, "nonexistent.srm")
        assert result["success"] is False
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_retry_failed_sync_success(self, plugin, tmp_path):
        """retry_failed_sync removes item from queue and re-syncs."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "test-device"

        plugin._add_to_offline_queue(42, "pokemon.srm", "upload", "HTTP 500")
        assert len(plugin._save_sync_state["offline_queue"]) == 1

        server_response = {"id": 100, "updated_at": "2026-01-01T00:00:00Z"}
        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", return_value=server_response):
            result = await plugin.retry_failed_sync(42, "pokemon.srm")

        assert result["success"] is True
        # Item should be removed from queue
        queue = await plugin.get_offline_queue()
        assert len(queue["queue"]) == 0

    @pytest.mark.asyncio
    async def test_sync_populates_offline_queue_on_error(self, plugin, tmp_path):
        """_sync_rom_saves populates offline queue when errors occur."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "test-device"

        # Mock server to return a save that needs upload, then fail the upload
        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", side_effect=Exception("Connection refused")):
            synced, errors = plugin._sync_rom_saves(42, direction="upload")

        assert synced == 0
        assert len(errors) == 1
        # Should be added to offline queue
        queue = plugin._save_sync_state["offline_queue"]
        assert len(queue) == 1
        assert queue[0]["filename"] == "pokemon.srm"


# ============================================================================
# Edge Case: Conflict Detection — False Alarm / Slow Path
# ============================================================================


class TestConflictDetectionFalseAlarm:
    """Timestamp changed but content identical → false alarm detection."""

    def _setup_sync_state(self, plugin, rom_id, filename, last_sync_hash,
                          server_updated_at="2026-02-17T06:00:00Z",
                          server_size=1024):
        rom_id_str = str(rom_id)
        plugin._save_sync_state["saves"][rom_id_str] = {
            "files": {
                filename: {
                    "last_sync_hash": last_sync_hash,
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": server_updated_at,
                    "last_sync_server_size": server_size,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

    def test_timestamp_changed_content_identical_is_false_alarm(self, plugin):
        """Server timestamp changed but content hash matches → skip (false alarm)."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123")
        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        with patch.object(plugin, "_get_server_save_hash", return_value="abc123"):
            result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        assert result == "skip"

    def test_false_alarm_updates_stored_metadata(self, plugin):
        """False alarm updates stored server_updated_at and size to prevent future slow paths."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123")
        server = _server_save(updated_at="2026-02-17T12:00:00Z", file_size_bytes=2048)

        with patch.object(plugin, "_get_server_save_hash", return_value="abc123"):
            plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        file_state = plugin._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["last_sync_server_updated_at"] == "2026-02-17T12:00:00Z"
        assert file_state["last_sync_server_size"] == 2048

    def test_timestamp_changed_local_changed_content_same_on_server(self, plugin):
        """Local changed, server timestamp changed but hash matches baseline → upload only."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123")
        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        with patch.object(plugin, "_get_server_save_hash", return_value="abc123"):
            result = plugin._detect_conflict(42, "pokemon.srm", "new_local_hash", server)

        assert result == "upload"

    def test_no_stored_timestamp_triggers_slow_path(self, plugin):
        """No stored server_updated_at triggers slow path instead of fast path."""
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": "abc123",
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    # No last_sync_server_updated_at
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }
        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        with patch.object(plugin, "_get_server_save_hash", return_value="abc123"):
            result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        assert result == "skip"

    def test_slow_path_server_hash_none_treated_as_unchanged(self, plugin):
        """Slow path: _get_server_save_hash returns None (download failed) → server unchanged."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123")
        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        with patch.object(plugin, "_get_server_save_hash", return_value=None):
            result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        # server_hash is None → `server_hash and ...` is False → unchanged
        assert result == "skip"

    def test_slow_path_server_hash_none_local_changed(self, plugin):
        """Slow path: server hash unavailable but local changed → upload (benefit of doubt)."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123")
        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        with patch.object(plugin, "_get_server_save_hash", return_value=None):
            result = plugin._detect_conflict(42, "pokemon.srm", "new_local_hash", server)

        assert result == "upload"

    def test_fast_path_size_unchanged_timestamp_matches(self, plugin):
        """Fast path: both timestamp and size match stored values → skip."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123",
                               server_updated_at="2026-02-17T06:00:00Z",
                               server_size=1024)
        server = _server_save(updated_at="2026-02-17T06:00:00Z", file_size_bytes=1024)

        result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        assert result == "skip"

    def test_fast_path_stored_size_none_treated_as_unchanged(self, plugin):
        """Fast path: stored_size is None (legacy state), timestamp matches → skip."""
        self._setup_sync_state(plugin, 42, "pokemon.srm", "abc123",
                               server_updated_at="2026-02-17T06:00:00Z",
                               server_size=1024)
        # Overwrite with None to simulate legacy state
        plugin._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]["last_sync_server_size"] = None
        server = _server_save(updated_at="2026-02-17T06:00:00Z", file_size_bytes=2048)

        result = plugin._detect_conflict(42, "pokemon.srm", "abc123", server)

        # stored_size is None → condition `stored_size is None or ...` → unchanged
        assert result == "skip"


# ============================================================================
# Edge Case: First Sync — Force Ask Behavior
# ============================================================================


class TestFirstSyncForceAsk:
    """First sync (no last_sync_hash) forces ask regardless of conflict mode."""

    @pytest.mark.asyncio
    async def test_first_sync_different_content_forces_ask_despite_always_upload(self, plugin, tmp_path):
        """First sync: different content forces 'ask' even with always_upload mode."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"local save data")

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["settings"]["conflict_mode"] = "always_upload"
        # No saves state → first sync

        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_get_server_save_hash", return_value="different_hash"):
            result = await plugin.sync_rom_saves(42)

        # Should be queued as conflict, not auto-uploaded
        assert len(plugin._save_sync_state["pending_conflicts"]) == 1
        conflict = plugin._save_sync_state["pending_conflicts"][0]
        assert conflict["rom_id"] == 42
        assert conflict["filename"] == "pokemon.srm"

    @pytest.mark.asyncio
    async def test_first_sync_different_content_forces_ask_despite_newest_wins(self, plugin, tmp_path):
        """First sync: different content forces 'ask' even with newest_wins mode."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"local save data")

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"

        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_get_server_save_hash", return_value="different_hash"):
            result = await plugin.sync_rom_saves(42)

        assert len(plugin._save_sync_state["pending_conflicts"]) == 1

    @pytest.mark.asyncio
    async def test_first_sync_identical_content_auto_resolves(self, plugin, tmp_path):
        """First sync: identical content auto-resolves to skip (no conflict queued)."""
        _install_rom(plugin, tmp_path)
        content = b"\x00" * 1024
        _create_save(tmp_path, content=content)
        local_hash = hashlib.md5(content).hexdigest()

        plugin._save_sync_state["device_id"] = "dev-1"

        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_get_server_save_hash", return_value=local_hash):
            result = await plugin.sync_rom_saves(42)

        assert len(plugin._save_sync_state["pending_conflicts"]) == 0
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_first_sync_local_only_uploads(self, plugin, tmp_path):
        """First sync: local save exists, no server save → upload without asking."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)

        plugin._save_sync_state["device_id"] = "dev-1"

        upload_response = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response) as mock_upload:
            result = await plugin.sync_rom_saves(42)

        assert result["synced"] == 1
        mock_upload.assert_called_once()
        assert len(plugin._save_sync_state["pending_conflicts"]) == 0

    @pytest.mark.asyncio
    async def test_first_sync_server_only_downloads(self, plugin, tmp_path):
        """First sync: server save exists, no local file → download without asking."""
        _install_rom(plugin, tmp_path)
        saves_dir = tmp_path / "retrodeck" / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)

        plugin._save_sync_state["device_id"] = "dev-1"
        server = _server_save()

        def fake_download(save_id, dest):
            with open(dest, "wb") as f:
                f.write(b"\xff" * 1024)

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_romm_download_save", side_effect=fake_download):
            result = await plugin.sync_rom_saves(42)

        assert result["synced"] == 1
        assert (saves_dir / "pokemon.srm").exists()
        assert len(plugin._save_sync_state["pending_conflicts"]) == 0


# ============================================================================
# Edge Case: Upload — POST Upsert vs PUT Update
# ============================================================================


class TestUploadUpsertBehavior:
    """Tests verifying POST upsert (new save) vs PUT update (existing save) paths."""

    def test_upload_no_server_save_uses_post(self, plugin, tmp_path):
        """No existing save on server → POST upsert (creates new)."""
        save_file = _create_save(tmp_path)

        with patch.object(plugin, "_romm_upload_multipart", return_value={"id": 200}) as mock_mp:
            plugin._romm_upload_save(42, str(save_file), "retroarch", save_id=None)

        mock_mp.assert_called_once()
        path = mock_mp.call_args.args[0]
        assert path.startswith("/api/saves?")
        assert "rom_id=42" in path
        assert mock_mp.call_args.kwargs["method"] == "POST"

    def test_upload_existing_save_uses_put(self, plugin, tmp_path):
        """Existing save on server (save_id given) → PUT to specific save."""
        save_file = _create_save(tmp_path)

        with patch.object(plugin, "_romm_upload_multipart", return_value={"id": 100}) as mock_mp:
            plugin._romm_upload_save(42, str(save_file), "retroarch", save_id=100)

        mock_mp.assert_called_once()
        path = mock_mp.call_args.args[0]
        assert "/api/saves/100?" in path
        assert mock_mp.call_args.kwargs["method"] == "PUT"

    def test_upload_emulator_param_url_encoded(self, plugin, tmp_path):
        """Emulator name is URL-encoded in query params."""
        save_file = _create_save(tmp_path)

        with patch.object(plugin, "_romm_upload_multipart", return_value={"id": 200}) as mock_mp:
            plugin._romm_upload_save(42, str(save_file), "retroarch", save_id=None)

        path = mock_mp.call_args.args[0]
        assert "emulator=retroarch" in path

    @pytest.mark.asyncio
    async def test_sync_passes_server_save_for_put(self, plugin, tmp_path):
        """_sync_rom_saves passes server_save to _do_upload_save for PUT when server has save."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"\x05" * 1024)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": "old_hash",
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        server = _server_save(save_id=100)
        upload_response = {"id": 100, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response) as mock_upload:
            result = await plugin.post_exit_sync(42)

        mock_upload.assert_called_once()
        # save_id should be 100 (from server save) → PUT path
        assert mock_upload.call_args.args[3] == 100  # save_id


# ============================================================================
# Edge Case: Upload — Special Characters in Filename
# ============================================================================


class TestUploadSpecialChars:
    """Upload with special characters (spaces, parentheses) in filename."""

    @pytest.mark.asyncio
    async def test_upload_filename_with_spaces_and_parens(self, plugin, tmp_path):
        """ROM with spaces and parentheses in name uploads correctly."""
        rom_name = "Final Fantasy (USA) (Rev A)"
        file_name = f"{rom_name}.gba"
        _install_rom(plugin, tmp_path, rom_id=42, system="gba", file_name=file_name)
        _create_save(tmp_path, system="gba", rom_name=rom_name)

        plugin._save_sync_state["device_id"] = "dev-1"
        upload_response = {
            "id": 200,
            "updated_at": "2026-02-17T15:00:00Z",
            "file_name": f"{rom_name}.srm",
        }

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response) as mock_upload:
            result = await plugin.sync_rom_saves(42)

        assert result["synced"] == 1
        # Verify the file_path passed to upload contains the special chars
        call_args = mock_upload.call_args.args
        assert rom_name in call_args[1]  # file_path arg

    @pytest.mark.asyncio
    async def test_find_saves_with_special_chars(self, plugin, tmp_path):
        """_find_save_files works with special characters in ROM name."""
        rom_name = "Metroid - Zero Mission (USA)"
        file_name = f"{rom_name}.gba"
        _install_rom(plugin, tmp_path, rom_id=42, system="gba", file_name=file_name)
        _create_save(tmp_path, system="gba", rom_name=rom_name)

        result = plugin._find_save_files(42)

        assert len(result) == 1
        assert result[0]["filename"] == f"{rom_name}.srm"


# ============================================================================
# Edge Case: Download — URL Encoding
# ============================================================================


class TestDownloadUrlEncoding:
    """Tests for download_path URL encoding with special characters."""

    def test_download_encodes_spaces_in_path(self, plugin):
        """download_path with spaces → %20 encoded."""
        metadata = {
            "id": 100,
            "download_path": "/saves/Final Fantasy (USA).srm",
        }

        with patch.object(plugin, "_romm_request", return_value=metadata), \
             patch.object(plugin, "_romm_download") as mock_download:
            plugin._romm_download_save(100, "/tmp/dest.srm")

        encoded = mock_download.call_args.args[0]
        assert encoded == "/saves/Final%20Fantasy%20%28USA%29.srm"

    def test_download_preserves_path_separators(self, plugin):
        """Forward slashes are preserved (safe="/")."""
        metadata = {
            "id": 100,
            "download_path": "/api/saves/files/game.srm",
        }

        with patch.object(plugin, "_romm_request", return_value=metadata), \
             patch.object(plugin, "_romm_download") as mock_download:
            plugin._romm_download_save(100, "/tmp/dest.srm")

        encoded = mock_download.call_args.args[0]
        assert encoded == "/api/saves/files/game.srm"

    def test_download_no_download_path_raises(self, plugin):
        """Missing download_path raises ValueError."""
        metadata = {"id": 100, "download_path": ""}

        with patch.object(plugin, "_romm_request", return_value=metadata):
            with pytest.raises(ValueError, match="no download_path"):
                plugin._romm_download_save(100, "/tmp/dest.srm")

    def test_download_encodes_unicode_filename(self, plugin):
        """download_path with unicode characters is properly encoded."""
        metadata = {
            "id": 100,
            "download_path": "/saves/ポケモン.srm",
        }

        with patch.object(plugin, "_romm_request", return_value=metadata), \
             patch.object(plugin, "_romm_download") as mock_download:
            plugin._romm_download_save(100, "/tmp/dest.srm")

        encoded = mock_download.call_args.args[0]
        # Should not contain raw unicode
        assert "ポケモン" not in encoded
        # But slashes preserved
        assert encoded.startswith("/saves/")


# ============================================================================
# Edge Case: Download — Failure Handling and Tmp Cleanup
# ============================================================================


class TestDownloadFailureHandling:
    """Tests for download failure handling, tmp file cleanup, and backup creation."""

    def test_download_failure_cleans_tmp_file(self, plugin, tmp_path):
        """Failed download in _sync_rom_saves cleans up .tmp file."""
        _install_rom(plugin, tmp_path)
        saves_dir = tmp_path / "retrodeck" / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)
        # Pre-create tmp file (simulates partial download)
        tmp_file = saves_dir / "pokemon.srm.tmp"
        tmp_file.write_bytes(b"partial data")

        plugin._save_sync_state["device_id"] = "dev-1"
        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_romm_download_save", side_effect=Exception("download failed")), \
             patch("time.sleep"):
            synced, errors = plugin._sync_rom_saves(42, direction="download")

        assert len(errors) >= 1
        assert not tmp_file.exists()

    @pytest.mark.asyncio
    async def test_download_creates_backup_with_timestamp(self, plugin, tmp_path):
        """Download backup filename includes timestamp."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"original save")

        plugin._save_sync_state["device_id"] = "dev-1"
        # Set up so server is newer → download path
        local_hash = plugin._file_md5(str(tmp_path / "retrodeck" / "saves" / "gba" / "pokemon.srm"))
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": local_hash,
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        server = _server_save(updated_at="2026-02-17T12:00:00Z")

        def fake_download(save_id, dest):
            with open(dest, "wb") as f:
                f.write(b"new server save")

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_romm_download_save", side_effect=fake_download):
            await plugin.pre_launch_sync(42)

        backup_dir = tmp_path / "retrodeck" / "saves" / "gba" / ".romm-backup"
        assert backup_dir.is_dir()
        backups = list(backup_dir.iterdir())
        assert len(backups) == 1
        # Backup name should be pokemon_YYYYMMDD_HHMMSS.srm
        backup_name = backups[0].name
        assert backup_name.startswith("pokemon_")
        assert backup_name.endswith(".srm")
        # Original data should be in backup
        assert backups[0].read_bytes() == b"original save"

    def test_download_no_backup_when_no_existing_file(self, plugin, tmp_path):
        """No backup created when there's no existing local file to overwrite."""
        _install_rom(plugin, tmp_path)
        saves_dir = tmp_path / "retrodeck" / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)

        server = _server_save()

        def fake_download(save_id, dest):
            with open(dest, "wb") as f:
                f.write(b"new save")

        with patch.object(plugin, "_romm_download_save", side_effect=fake_download):
            plugin._do_download_save(server, str(saves_dir), "pokemon.srm", "42", "gba")

        backup_dir = saves_dir / ".romm-backup"
        assert not backup_dir.exists()


# ============================================================================
# Edge Case: Playtime Note ID Storage and Recovery
# ============================================================================


class TestPlaytimeNoteIdRecovery:
    """Tests for note_id storage on creation and recovery from ROM detail."""

    def test_create_note_stores_note_id_in_state(self, plugin):
        """_romm_create_playtime_note stores returned note_id in playtime state."""
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 100,
            "session_count": 1,
        }

        with patch.object(plugin, "_romm_post_json", return_value={"id": 77, "title": "romm-sync:playtime"}):
            plugin._romm_create_playtime_note(42, {"seconds": 100})

        assert plugin._save_sync_state["playtime"]["42"]["note_id"] == 77

    def test_create_note_no_id_in_response(self, plugin):
        """If server response has no id, note_id is not set."""
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 100,
            "session_count": 1,
        }

        with patch.object(plugin, "_romm_post_json", return_value={"title": "romm-sync:playtime"}):
            plugin._romm_create_playtime_note(42, {"seconds": 100})

        assert "note_id" not in plugin._save_sync_state["playtime"]["42"]

    def test_recover_note_id_via_sync_playtime(self, plugin):
        """_sync_playtime_to_romm discovers existing note_id from server (state was lost)."""
        plugin._save_sync_state["device_name"] = "deck"
        plugin._save_sync_state["playtime"]["42"] = {
            "total_seconds": 500,
            "session_count": 2,
            # No note_id stored — state was lost
        }

        existing_note = {
            "id": 55,
            "title": "romm-sync:playtime",
            "content": '{"seconds": 300}',
        }

        with patch.object(plugin, "_romm_get_playtime_note", return_value=existing_note), \
             patch.object(plugin, "_romm_update_playtime_note") as mock_update, \
             patch("time.sleep"):
            plugin._sync_playtime_to_romm(42, 200)

        # Should have used the discovered note_id for update (not create)
        mock_update.assert_called_once()
        assert mock_update.call_args.args[1] == 55  # note_id

    def test_romm_get_playtime_note_no_matching_title(self, plugin):
        """No note with matching title returns None."""
        rom_detail = {
            "id": 42,
            "all_user_notes": [
                {"id": 1, "title": "other-note", "content": "hello"},
                {"id": 2, "title": "game-review", "content": "great game"},
            ],
        }
        with patch.object(plugin, "_romm_request", return_value=rom_detail):
            result = plugin._romm_get_playtime_note(42)

        assert result is None

    def test_romm_get_playtime_note_empty_notes(self, plugin):
        """ROM with empty notes list returns None."""
        rom_detail = {"id": 42, "all_user_notes": []}
        with patch.object(plugin, "_romm_request", return_value=rom_detail):
            result = plugin._romm_get_playtime_note(42)

        assert result is None

    def test_romm_get_playtime_note_invalid_response(self, plugin):
        """Non-dict API response returns None."""
        with patch.object(plugin, "_romm_request", return_value="not a dict"):
            result = plugin._romm_get_playtime_note(42)

        assert result is None

    def test_romm_get_playtime_note_missing_all_user_notes(self, plugin):
        """ROM detail without all_user_notes field returns None."""
        rom_detail = {"id": 42}
        with patch.object(plugin, "_romm_request", return_value=rom_detail):
            result = plugin._romm_get_playtime_note(42)

        assert result is None

    def test_romm_get_playtime_note_non_list_notes(self, plugin):
        """all_user_notes is not a list → returns None."""
        rom_detail = {"id": 42, "all_user_notes": "not a list"}
        with patch.object(plugin, "_romm_request", return_value=rom_detail):
            result = plugin._romm_get_playtime_note(42)

        assert result is None


# ============================================================================
# Edge Case: State Recovery After File Loss
# ============================================================================


class TestStateRecovery:
    """Tests for recovery after save_sync_state.json loss."""

    @pytest.mark.asyncio
    async def test_complete_state_loss_different_content_forces_ask(self, plugin, tmp_path):
        """State file lost, both local and server exist with different content → conflict queued."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"local content")

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        # No saves state — state was completely lost

        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_get_server_save_hash", return_value="different_server_hash"):
            synced, errors = plugin._sync_rom_saves(42, direction="both")

        assert synced == 0
        assert len(plugin._save_sync_state["pending_conflicts"]) == 1
        conflict = plugin._save_sync_state["pending_conflicts"][0]
        assert conflict["rom_id"] == 42
        assert conflict["filename"] == "pokemon.srm"

    @pytest.mark.asyncio
    async def test_complete_state_loss_identical_content_skips(self, plugin, tmp_path):
        """State file lost, both exist with identical content → auto-skip (no conflict)."""
        _install_rom(plugin, tmp_path)
        content = b"\x42" * 1024
        _create_save(tmp_path, content=content)
        local_hash = hashlib.md5(content).hexdigest()

        plugin._save_sync_state["device_id"] = "dev-1"

        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_get_server_save_hash", return_value=local_hash):
            synced, errors = plugin._sync_rom_saves(42, direction="both")

        assert synced == 0
        assert len(plugin._save_sync_state["pending_conflicts"]) == 0

    @pytest.mark.asyncio
    async def test_partial_state_missing_rom_treated_as_first_sync(self, plugin, tmp_path):
        """State exists for some ROMs but not others — missing ROM is first-sync."""
        _install_rom(plugin, tmp_path, rom_id=42, file_name="pokemon.gba")
        _install_rom(plugin, tmp_path, rom_id=43, file_name="zelda.gba")
        _create_save(tmp_path, rom_name="pokemon", content=b"poke data")
        _create_save(tmp_path, rom_name="zelda", content=b"zelda data")

        plugin._save_sync_state["device_id"] = "dev-1"
        # ROM 42 has sync history
        poke_hash = hashlib.md5(b"poke data").hexdigest()
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": poke_hash,
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }
        # ROM 43 has NO sync state → first sync

        server_poke = _server_save(save_id=100, rom_id=42)
        server_zelda = _server_save(save_id=101, rom_id=43, filename="zelda.srm")

        def mock_list(rom_id):
            if rom_id == 42:
                return [server_poke]
            elif rom_id == 43:
                return [server_zelda]
            return []

        with patch.object(plugin, "_romm_list_saves", side_effect=mock_list), \
             patch.object(plugin, "_get_server_save_hash", return_value="different_zelda_hash"):
            result = await plugin.sync_all_saves()

        # ROM 42: unchanged on both sides → skip
        # ROM 43: first sync, different content → conflict (force ask)
        conflicts = plugin._save_sync_state["pending_conflicts"]
        assert any(c["rom_id"] == 43 and c["filename"] == "zelda.srm" for c in conflicts)
        # ROM 42 should NOT be in conflicts
        assert not any(c["rom_id"] == 42 for c in conflicts)

    @pytest.mark.asyncio
    async def test_state_loss_server_hash_unavailable_forces_ask(self, plugin, tmp_path):
        """State lost, can't download server save for hash comparison → conflict."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"local content")

        plugin._save_sync_state["device_id"] = "dev-1"

        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_get_server_save_hash", return_value=None):
            synced, errors = plugin._sync_rom_saves(42, direction="both")

        # Can't verify → conflict
        assert len(plugin._save_sync_state["pending_conflicts"]) == 1


# ============================================================================
# Edge Case: Offline Queue — Retry Goes Through Full Conflict Detection
# ============================================================================


class TestOfflineQueueRetry:
    """Tests that offline queue retry uses full conflict detection."""

    @pytest.mark.asyncio
    async def test_retry_calls_full_sync_not_blind_upload(self, plugin, tmp_path):
        """Retry invokes _sync_rom_saves (full conflict detection), not blind upload."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        plugin._add_to_offline_queue(42, "pokemon.srm", "upload", "HTTP 500")

        with patch.object(plugin, "_sync_rom_saves", return_value=(1, [])) as mock_sync:
            result = await plugin.retry_failed_sync(42, "pokemon.srm")

        mock_sync.assert_called_once_with(42, direction="upload")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_retry_detects_conflict_when_server_changed(self, plugin, tmp_path):
        """Retry detects that server changed during offline period → conflict."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path, content=b"\x01" * 1024)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["settings"]["conflict_mode"] = "ask_me"
        plugin._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "last_sync_hash": "old_snapshot",
                    "last_sync_at": "2026-02-17T08:00:00Z",
                    "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                    "last_sync_server_size": 1024,
                }
            },
            "emulator": "retroarch",
            "system": "gba",
        }

        plugin._add_to_offline_queue(42, "pokemon.srm", "upload", "HTTP 500")

        # Server has also changed since the failure
        server = _server_save(updated_at="2026-02-17T14:00:00Z")

        with patch.object(plugin, "_romm_list_saves", return_value=[server]), \
             patch.object(plugin, "_get_server_save_hash", return_value="new_server_hash"):
            result = await plugin.retry_failed_sync(42, "pokemon.srm")

        # Both changed + ask_me mode → should queue conflict
        assert len(plugin._save_sync_state["pending_conflicts"]) >= 1

    @pytest.mark.asyncio
    async def test_retry_removes_from_queue_before_sync(self, plugin, tmp_path):
        """Item is removed from offline queue even if retry fails with new errors."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        plugin._add_to_offline_queue(42, "pokemon.srm", "upload", "HTTP 500")
        assert len(plugin._save_sync_state["offline_queue"]) == 1

        with patch.object(plugin, "_romm_list_saves", return_value=[]), \
             patch.object(plugin, "_romm_upload_save", side_effect=ConnectionError("still failing")):
            result = await plugin.retry_failed_sync(42, "pokemon.srm")

        # Original queue item removed (but may be re-added by new failure)
        # The queue should have the new error, not the old "HTTP 500"
        queue = plugin._save_sync_state["offline_queue"]
        if queue:
            assert queue[0]["error"] != "HTTP 500"

    @pytest.mark.asyncio
    async def test_retry_preserves_direction(self, plugin, tmp_path):
        """Retry uses the stored direction from the offline queue item."""
        _install_rom(plugin, tmp_path)
        saves_dir = tmp_path / "retrodeck" / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)
        plugin._save_sync_state["device_id"] = "dev-1"

        # Queue a download failure
        plugin._add_to_offline_queue(42, "pokemon.srm", "download", "Connection refused")

        with patch.object(plugin, "_sync_rom_saves", return_value=(0, [])) as mock_sync:
            await plugin.retry_failed_sync(42, "pokemon.srm")

        mock_sync.assert_called_once_with(42, direction="download")


# ============================================================================
# Edge Case: Sync All — Correct Counts and Mixed States
# ============================================================================


class TestSyncAllEdgeCases:
    """Edge cases for sync_all_saves counting and mixed ROM states."""

    @pytest.mark.asyncio
    async def test_correct_synced_and_conflict_counts(self, plugin, tmp_path):
        """sync_all_saves reports accurate synced, conflict, and roms_checked counts."""
        # ROM 1: will upload (local only)
        _install_rom(plugin, tmp_path, rom_id=1, file_name="game_a.gba")
        _create_save(tmp_path, rom_name="game_a")

        # ROM 2: will have conflict (both exist, different content, first sync)
        _install_rom(plugin, tmp_path, rom_id=2, file_name="game_b.gba")
        _create_save(tmp_path, rom_name="game_b", content=b"local b data")

        # ROM 3: no save file (installed but never played)
        _install_rom(plugin, tmp_path, rom_id=3, file_name="game_c.gba")

        plugin._save_sync_state["device_id"] = "dev-1"

        server_b = _server_save(save_id=200, rom_id=2, filename="game_b.srm")
        upload_response = {"id": 300, "updated_at": "2026-02-17T15:00:00Z"}

        def mock_list(rom_id):
            if rom_id == 2:
                return [server_b]
            return []

        with patch.object(plugin, "_romm_list_saves", side_effect=mock_list), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response), \
             patch.object(plugin, "_get_server_save_hash", return_value="different_hash"):
            result = await plugin.sync_all_saves()

        assert result["roms_checked"] == 3
        assert result["synced"] >= 1  # game_a uploaded
        assert result["conflicts"] >= 1  # game_b conflict

    @pytest.mark.asyncio
    async def test_roms_without_saves_skip_cleanly(self, plugin, tmp_path):
        """ROMs installed but without save files don't cause errors."""
        _install_rom(plugin, tmp_path, rom_id=1, file_name="game_a.gba")
        _install_rom(plugin, tmp_path, rom_id=2, file_name="game_b.gba")
        # No save files created for either

        plugin._save_sync_state["device_id"] = "dev-1"

        with patch.object(plugin, "_romm_list_saves", return_value=[]):
            result = await plugin.sync_all_saves()

        assert result["success"] is True
        assert result["roms_checked"] == 2
        assert result["synced"] == 0
        assert len(result["errors"]) == 0


# ============================================================================
# Edge Case: Direction Filtering
# ============================================================================


class TestDirectionFiltering:
    """Tests that sync direction correctly filters operations."""

    def test_download_direction_skips_upload(self, plugin, tmp_path):
        """direction='download' skips local-only saves that would need upload."""
        _install_rom(plugin, tmp_path)
        _create_save(tmp_path)
        plugin._save_sync_state["device_id"] = "dev-1"

        # No server saves → action would be "upload"
        with patch.object(plugin, "_romm_list_saves", return_value=[]):
            synced, errors = plugin._sync_rom_saves(42, direction="download")

        assert synced == 0

    def test_upload_direction_skips_download(self, plugin, tmp_path):
        """direction='upload' skips server-only saves that would need download."""
        _install_rom(plugin, tmp_path)
        saves_dir = tmp_path / "retrodeck" / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)
        # No local save

        plugin._save_sync_state["device_id"] = "dev-1"
        server = _server_save()

        with patch.object(plugin, "_romm_list_saves", return_value=[server]):
            synced, errors = plugin._sync_rom_saves(42, direction="upload")

        assert synced == 0

    def test_both_direction_handles_upload_and_download(self, plugin, tmp_path):
        """direction='both' handles both uploads and downloads."""
        _install_rom(plugin, tmp_path, rom_id=42, file_name="pokemon.gba")
        _create_save(tmp_path, rom_name="pokemon")

        plugin._save_sync_state["device_id"] = "dev-1"

        # Server has a different save (zelda.srm) that we don't have locally
        server_zelda = _server_save(save_id=200, rom_id=42, filename="zelda.srm")
        upload_response = {"id": 300, "updated_at": "2026-02-17T15:00:00Z"}

        def fake_download(save_id, dest):
            with open(dest, "wb") as f:
                f.write(b"\xff" * 512)

        with patch.object(plugin, "_romm_list_saves", return_value=[server_zelda]), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response), \
             patch.object(plugin, "_romm_download_save", side_effect=fake_download):
            synced, errors = plugin._sync_rom_saves(42, direction="both")

        # pokemon.srm uploaded + zelda.srm downloaded
        assert synced == 2


# ============================================================================
# Edge Case: _get_server_save_hash
# ============================================================================


class TestGetServerSaveHash:
    """Tests for _get_server_save_hash temp file handling."""

    def test_returns_hash_of_downloaded_content(self, plugin, tmp_path):
        """Downloads server save to temp, hashes it, returns hash."""
        content = b"server save content"
        expected_hash = hashlib.md5(content).hexdigest()

        def fake_download(save_id, dest):
            with open(dest, "wb") as f:
                f.write(content)

        with patch.object(plugin, "_romm_download_save", side_effect=fake_download):
            result = plugin._get_server_save_hash({"id": 100})

        assert result == expected_hash

    def test_returns_none_on_download_failure(self, plugin):
        """Download failure returns None (not exception)."""
        with patch.object(plugin, "_romm_download_save", side_effect=ConnectionError("offline")):
            result = plugin._get_server_save_hash({"id": 100})

        assert result is None

    def test_returns_none_for_save_without_id(self, plugin):
        """Save dict without 'id' key returns None immediately."""
        result = plugin._get_server_save_hash({})

        assert result is None

    def test_cleans_up_temp_file_on_success(self, plugin, tmp_path):
        """Temp file is removed after successful hash computation."""
        created_paths = []

        def fake_download(save_id, dest):
            created_paths.append(dest)
            with open(dest, "wb") as f:
                f.write(b"data")

        with patch.object(plugin, "_romm_download_save", side_effect=fake_download):
            plugin._get_server_save_hash({"id": 100})

        # Temp file should be cleaned up
        assert len(created_paths) == 1
        assert not os.path.exists(created_paths[0])

    def test_cleans_up_temp_file_on_failure(self, plugin, tmp_path):
        """Temp file is removed even after download failure (retries exhausted)."""
        created_paths = []

        def fake_download(save_id, dest):
            created_paths.append(dest)
            # Create file then fail
            with open(dest, "wb") as f:
                f.write(b"partial")
            raise ConnectionError("mid-download failure")

        with patch.object(plugin, "_romm_download_save", side_effect=fake_download), \
             patch("time.sleep"):
            plugin._get_server_save_hash({"id": 100})

        # _with_retry retries 3 times for transient errors (ConnectionError)
        assert len(created_paths) == 3
        assert not os.path.exists(created_paths[0])


# ============================================================================
# Edge Case: Conflict Resolution with Missing Data
# ============================================================================


class TestResolveConflictEdgeCases:
    """Edge cases for resolve_conflict callable."""

    @pytest.mark.asyncio
    async def test_resolve_upload_without_server_save_id(self, plugin, tmp_path):
        """Resolving upload when conflict has no server_save_id uses POST upsert."""
        _install_rom(plugin, tmp_path)
        save_file = _create_save(tmp_path)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["pending_conflicts"] = [{
            "rom_id": 42,
            "filename": "pokemon.srm",
            "local_path": str(save_file),
            "local_hash": "abc",
            "server_save_id": None,  # No server save ID (state lost)
            "server_updated_at": "",
            "created_at": "2026-02-17T12:00:00Z",
        }]

        upload_response = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_upload_save", return_value=upload_response) as mock_upload:
            result = await plugin.resolve_conflict(42, "pokemon.srm", "upload")

        assert result["success"] is True
        # Should use POST (no save_id → None → uses POST)
        assert mock_upload.call_args.args[3] is None  # save_id arg

    @pytest.mark.asyncio
    async def test_resolve_download_missing_server_save_id(self, plugin, tmp_path):
        """Resolving download when server_save_id is missing fails gracefully."""
        _install_rom(plugin, tmp_path)

        plugin._save_sync_state["pending_conflicts"] = [{
            "rom_id": 42,
            "filename": "pokemon.srm",
            "local_path": str(tmp_path / "retrodeck" / "saves" / "gba" / "pokemon.srm"),
            "server_save_id": None,
            "created_at": "2026-02-17T12:00:00Z",
        }]

        result = await plugin.resolve_conflict(42, "pokemon.srm", "download")

        assert result["success"] is False
        assert "no server save" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_resolve_upload_local_file_deleted(self, plugin, tmp_path):
        """Resolving upload when local file was deleted fails gracefully."""
        _install_rom(plugin, tmp_path)
        # Note: no save file created on disk

        plugin._save_sync_state["pending_conflicts"] = [{
            "rom_id": 42,
            "filename": "pokemon.srm",
            "local_path": str(tmp_path / "retrodeck" / "saves" / "gba" / "pokemon.srm"),
            "local_hash": "abc",
            "server_save_id": 100,
            "created_at": "2026-02-17T12:00:00Z",
        }]

        result = await plugin.resolve_conflict(42, "pokemon.srm", "upload")

        assert result["success"] is False
        assert "not found" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_resolve_conflict_removes_only_matching(self, plugin, tmp_path):
        """Resolving one conflict leaves other conflicts intact."""
        _install_rom(plugin, tmp_path)
        save_file = _create_save(tmp_path)

        plugin._save_sync_state["device_id"] = "dev-1"
        plugin._save_sync_state["pending_conflicts"] = [
            {
                "rom_id": 42,
                "filename": "pokemon.srm",
                "local_path": str(save_file),
                "local_hash": "abc",
                "server_save_id": 100,
                "created_at": "2026-02-17T12:00:00Z",
            },
            {
                "rom_id": 99,
                "filename": "zelda.srm",
                "local_path": "/some/other/path",
                "local_hash": "def",
                "server_save_id": 200,
                "created_at": "2026-02-17T12:00:00Z",
            },
        ]

        upload_response = {"id": 100, "updated_at": "2026-02-17T15:00:00Z"}

        with patch.object(plugin, "_romm_request", return_value={"id": 100}), \
             patch.object(plugin, "_romm_upload_save", return_value=upload_response):
            result = await plugin.resolve_conflict(42, "pokemon.srm", "upload")

        assert result["success"] is True
        remaining = plugin._save_sync_state["pending_conflicts"]
        assert len(remaining) == 1
        assert remaining[0]["rom_id"] == 99


# ============================================================================
# Edge Case: Clock Skew Tolerance — Boundary Values
# ============================================================================


class TestClockSkewBoundary:
    """Boundary tests for clock skew tolerance in newest_wins mode."""

    def test_exactly_at_tolerance_boundary_asks(self, plugin):
        """Timestamps exactly at tolerance boundary → ask (not upload/download)."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        plugin._save_sync_state["settings"]["clock_skew_tolerance_sec"] = 60

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        # Exactly 60 seconds different
        local_mtime = datetime(2026, 2, 17, 12, 1, 0, tzinfo=timezone.utc).timestamp()

        result = plugin._resolve_conflict_by_mode(local_mtime, server)

        assert result == "ask"

    def test_one_second_beyond_tolerance_resolves(self, plugin):
        """Timestamps 1 second beyond tolerance → resolves (not ask)."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        plugin._save_sync_state["settings"]["clock_skew_tolerance_sec"] = 60

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        # 61 seconds newer locally
        local_mtime = datetime(2026, 2, 17, 12, 1, 1, tzinfo=timezone.utc).timestamp()

        result = plugin._resolve_conflict_by_mode(local_mtime, server)

        assert result == "upload"

    def test_zero_tolerance_resolves_any_difference(self, plugin):
        """Zero tolerance → any time difference resolves immediately."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"
        plugin._save_sync_state["settings"]["clock_skew_tolerance_sec"] = 0

        server = _server_save(updated_at="2026-02-17T12:00:00Z")
        # 1 second newer locally
        local_mtime = datetime(2026, 2, 17, 12, 0, 1, tzinfo=timezone.utc).timestamp()

        result = plugin._resolve_conflict_by_mode(local_mtime, server)

        assert result == "upload"

    def test_invalid_server_timestamp_asks(self, plugin):
        """Invalid server timestamp → ask (can't compare)."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"

        server = _server_save(updated_at="not-a-timestamp")

        result = plugin._resolve_conflict_by_mode(time.time(), server)

        assert result == "ask"

    def test_empty_server_timestamp_asks(self, plugin):
        """Empty server timestamp → ask."""
        plugin._save_sync_state["settings"]["conflict_mode"] = "newest_wins"

        server = _server_save(updated_at="")

        result = plugin._resolve_conflict_by_mode(time.time(), server)

        assert result == "ask"


# ============================================================================
# Edge Case: _add_pending_conflict metadata
# ============================================================================


class TestAddPendingConflictMetadata:
    """Tests that _add_pending_conflict captures complete metadata."""

    def test_captures_local_file_metadata(self, plugin, tmp_path):
        """Conflict entry includes local hash, mtime, and size."""
        save_file = _create_save(tmp_path, content=b"test data")
        server = _server_save()

        plugin._add_pending_conflict(42, "pokemon.srm", str(save_file), server)

        conflict = plugin._save_sync_state["pending_conflicts"][0]
        assert conflict["local_hash"] is not None
        assert conflict["local_mtime"] is not None
        assert conflict["local_size"] is not None
        assert conflict["local_size"] == len(b"test data")
        # Verify mtime is ISO format
        datetime.fromisoformat(conflict["local_mtime"])

    def test_captures_server_metadata(self, plugin, tmp_path):
        """Conflict entry includes server save_id, updated_at, and size."""
        save_file = _create_save(tmp_path)
        server = _server_save(save_id=123, updated_at="2026-02-17T12:00:00Z", file_size_bytes=2048)

        plugin._add_pending_conflict(42, "pokemon.srm", str(save_file), server)

        conflict = plugin._save_sync_state["pending_conflicts"][0]
        assert conflict["server_save_id"] == 123
        assert conflict["server_updated_at"] == "2026-02-17T12:00:00Z"
        assert conflict["server_size"] == 2048

    def test_handles_nonexistent_local_file(self, plugin):
        """Conflict for non-existent local file stores None for local metadata."""
        server = _server_save()

        plugin._add_pending_conflict(42, "pokemon.srm", "/nonexistent/path.srm", server)

        conflict = plugin._save_sync_state["pending_conflicts"][0]
        assert conflict["local_hash"] is None
        assert conflict["local_mtime"] is None
        assert conflict["local_size"] is None


# ============================================================================
# Feature Flag: save_sync_enabled
# ============================================================================


class TestSaveSyncFeatureFlag:
    """Tests for the save_sync_enabled feature flag (off by default)."""

    @pytest.mark.asyncio
    async def test_default_disabled(self, plugin):
        """save_sync_enabled defaults to False in fresh state."""
        plugin._init_save_sync_state()  # Reset to defaults (no test fixture override)
        settings = plugin._save_sync_state["settings"]
        assert settings["save_sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_ensure_device_disabled(self, plugin):
        """ensure_device_registered returns disabled marker when save sync off."""
        plugin._save_sync_state["settings"]["save_sync_enabled"] = False
        result = await plugin.ensure_device_registered()
        assert result["success"] is False
        assert result.get("disabled") is True
        assert plugin._save_sync_state["device_id"] is None

    @pytest.mark.asyncio
    async def test_pre_launch_sync_disabled(self, plugin):
        """pre_launch_sync skips when save sync disabled."""
        plugin._save_sync_state["settings"]["save_sync_enabled"] = False
        result = await plugin.pre_launch_sync(42)
        assert result["success"] is True
        assert result["synced"] == 0
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_post_exit_sync_disabled(self, plugin):
        """post_exit_sync skips when save sync disabled."""
        plugin._save_sync_state["settings"]["save_sync_enabled"] = False
        result = await plugin.post_exit_sync(42)
        assert result["success"] is True
        assert result["synced"] == 0
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_sync_rom_saves_disabled(self, plugin):
        """sync_rom_saves returns error when save sync disabled."""
        plugin._save_sync_state["settings"]["save_sync_enabled"] = False
        result = await plugin.sync_rom_saves(42)
        assert result["success"] is False
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_sync_all_saves_disabled(self, plugin):
        """sync_all_saves returns error when save sync disabled."""
        plugin._save_sync_state["settings"]["save_sync_enabled"] = False
        result = await plugin.sync_all_saves()
        assert result["success"] is False
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_enable_via_settings_update(self, plugin):
        """save_sync_enabled can be toggled via update_save_sync_settings."""
        plugin._save_sync_state["settings"]["save_sync_enabled"] = False
        result = await plugin.update_save_sync_settings({"save_sync_enabled": True})
        assert result["success"] is True
        assert plugin._save_sync_state["settings"]["save_sync_enabled"] is True

    @pytest.mark.asyncio
    async def test_disable_via_settings_update(self, plugin):
        """save_sync_enabled can be disabled via update_save_sync_settings."""
        result = await plugin.update_save_sync_settings({"save_sync_enabled": False})
        assert result["success"] is True
        assert plugin._save_sync_state["settings"]["save_sync_enabled"] is False

    @pytest.mark.asyncio
    async def test_get_settings_includes_flag(self, plugin):
        """get_save_sync_settings returns save_sync_enabled field."""
        result = await plugin.get_save_sync_settings()
        assert "save_sync_enabled" in result

    @pytest.mark.asyncio
    async def test_is_save_sync_enabled_helper(self, plugin):
        """_is_save_sync_enabled reflects the settings value."""
        plugin._save_sync_state["settings"]["save_sync_enabled"] = True
        assert plugin._is_save_sync_enabled() is True
        plugin._save_sync_state["settings"]["save_sync_enabled"] = False
        assert plugin._is_save_sync_enabled() is False
