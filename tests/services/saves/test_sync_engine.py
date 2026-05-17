"""Tests for SyncEngine — newest-wins sync matrix and concurrent-sync coordination."""

import hashlib
import logging
import os

import pytest

from domain.save_state import FileSyncState, RomSaveState
from lib.errors import RommApiError
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _file_md5,
    _install_rom,
    _server_save,
    _server_save_with_syncs,
    make_service,
)


class TestSyncRomSaves:
    def test_local_only_uploads(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"save data")

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)
        assert synced == 1
        assert errors == []
        assert conflicts == []
        assert any(c[0] == "upload_save" for c in fake.call_log)

    def test_server_only_downloads(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        # Add server save but no local file
        ss = _server_save()
        fake.saves[100] = ss

        synced, errors, _ = svc._sync_engine._sync_rom_saves(42)
        assert synced == 1
        assert errors == []
        # Verify the file was downloaded
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()

    def test_rom_not_installed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        synced, errors, _ = svc._sync_engine._sync_rom_saves(999)
        assert synced == 0
        assert errors == []

    def test_api_error_on_list_saves(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        fake.fail_on_next(RommApiError("Server error"))

        synced, errors, _ = svc._sync_engine._sync_rom_saves(42)
        assert synced == 0
        assert len(errors) == 1
        assert "Failed to fetch saves" in errors[0]

    # ------------------------------------------------------------------
    # Regression tests for issue #238 — pending-migration handling.
    # Rule 2: skip server_only downloads while a save-sort migration is
    # pending so the mtime-naive resolver cannot prefer freshly-downloaded
    # stale server content over real user progress at the other layout.
    # ------------------------------------------------------------------

    def test_sync_rom_saves_skips_server_only_downloads_during_pending_migration(self, tmp_path):
        """server_only matches must be skipped while migration is pending (#238)."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        # Mark migration pending — detect has fired, user hasn't resolved yet.
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": False}
        svc._state["save_sort_settings_previous"] = {"sort_by_content": True, "sort_by_core": False}
        # Server has a save, no local file anywhere.
        ss = _server_save()
        fake.saves[100] = ss

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 0
        assert errors == []
        assert conflicts == []
        # No download was initiated.
        assert fake.downloaded_files == {}
        # No file landed on disk under either layout.
        saves_dir = tmp_path / "saves" / "gba"
        assert not (saves_dir / "pokemon.srm").exists()

    def test_sync_rom_saves_uploads_local_only_during_pending_migration(self, tmp_path):
        """local_only matches must still upload during pending migration (#238)."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": False}
        svc._state["save_sort_settings_previous"] = {"sort_by_content": True, "sort_by_core": False}
        # Local save at the (previous == current, same layout) location.
        _create_save(tmp_path, content=b"user progress")

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        # Upload went through.
        assert any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_rom_saves_invokes_detect_sort_change_before_sync(self, tmp_path):
        """Manual sync_rom_saves must also refresh save-sort state first (#238).

        Without the detect-first call, a user editing retroarch.cfg outside
        of a session and then triggering manual sync would race the same
        way that direct-Steam-launch does — sync would compute saves_dir
        from stale state and risk landing stale server content at the
        wrong layout.
        """
        call_order: list[str] = []

        def fake_detect() -> None:
            call_order.append("detect")

        svc, _ = make_service(tmp_path, detect_sort_change=fake_detect)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        orig_sync = svc._sync_engine._sync_rom_saves

        def wrapped_sync(rom_id):
            call_order.append("sync")
            return orig_sync(rom_id)

        svc._sync_engine._sync_rom_saves = wrapped_sync  # type: ignore[method-assign]

        result = await svc.sync_rom_saves(42)

        assert result["success"] is True
        # detect fired exactly once, before sync ran.
        assert call_order.count("detect") == 1
        assert call_order.index("detect") < call_order.index("sync")

    @pytest.mark.asyncio
    async def test_sync_rom_saves_message_includes_conflict_count(self, tmp_path):
        """Public sync_rom_saves must surface conflict count in its message.

        Previously reported "Synced 0 save(s)" even with conflicts present,
        which reads as success — user had no signal that manual intervention
        was needed.
        """
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)

        # Stub _sync_rom_saves to return 1 conflict, 0 synced, 0 errors
        def stub_sync(rom_id):
            return (0, [], [{"type": "newer_in_slot", "rom_id": rom_id}])

        svc._sync_engine._sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.sync_rom_saves(42)

        # success is still True — conflicts are legitimate state, not technical failure
        assert result["success"] is True
        assert "1 conflict(s)" in result["message"]
        assert result["synced"] == 0


class TestSyncAllSaves:
    @pytest.mark.asyncio
    async def test_syncs_multiple_roms(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"

        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"save2")

        result = await svc.sync_all_saves()
        assert result["success"] is True
        assert result["synced"] == 2
        assert result["roms_checked"] == 2

    @pytest.mark.asyncio
    async def test_disabled_returns_early(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.sync_all_saves()
        assert result["success"] is False
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_partial_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"

        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"save2")

        # Make the second ROM's list_saves fail
        original_list = fake.list_saves

        call_count = 0

        def flaky_list(rom_id, *, device_id=None, slot=None):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RommApiError("Server error")
            return original_list(rom_id, device_id=device_id, slot=slot)

        fake.list_saves = flaky_list

        result = await svc.sync_all_saves()
        assert result["synced"] >= 1
        assert len(result["errors"]) >= 1

    @pytest.mark.asyncio
    async def test_sync_all_saves_invokes_detect_sort_change_before_sync(self, tmp_path):
        """Manual sync_all_saves must also refresh save-sort state first (#238).

        Same race as sync_rom_saves but for the bulk path: detect must
        fire once at the top of the method, before any per-ROM sync runs.
        """
        call_order: list[str] = []

        def fake_detect() -> None:
            call_order.append("detect")

        svc, _ = make_service(tmp_path, detect_sort_change=fake_detect)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")

        orig_sync = svc._sync_engine._sync_rom_saves

        def wrapped_sync(rom_id):
            call_order.append("sync")
            return orig_sync(rom_id)

        svc._sync_engine._sync_rom_saves = wrapped_sync  # type: ignore[method-assign]

        result = await svc.sync_all_saves()

        assert result["success"] is True
        # detect fired exactly once, before any per-ROM sync ran.
        assert call_order.count("detect") == 1
        assert call_order.index("detect") < call_order.index("sync")

    @pytest.mark.asyncio
    async def test_sync_all_saves_success_stays_true_with_only_conflicts(self, tmp_path):
        """Regression guard: success flag reflects errors only, not conflicts.

        Conflicts are a legitimate state requiring user resolution — not a
        technical failure. Frontend distinguishes via conflicts count; success
        flag must stay reserved for actual errors.
        """
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")

        # Stub internal sync to produce conflicts but no errors
        def stub_sync(rom_id):
            return (0, [], [{"type": "newer_in_slot", "rom_id": rom_id}])

        svc._sync_engine._sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.sync_all_saves()

        assert result["success"] is True
        assert result["conflicts"] >= 1
        assert "conflict(s)" in result["message"]


class TestPreLaunchSync:
    @pytest.mark.asyncio
    async def test_downloads_server_saves(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)

        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.pre_launch_sync(42)
        assert result["success"] is True
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_disabled_skips(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.pre_launch_sync(42)
        assert result["success"] is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_pre_launch_disabled_in_settings(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.settings.sync_before_launch = False
        svc._save_sync_state.device_id = "test-device"

        result = await svc.pre_launch_sync(42)
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_pre_launch_sync_invokes_detect_sort_change_before_migration_gate(self, tmp_path):
        """detect_sort_change is called before the _is_save_sort_changed gate (#238)."""
        order: list[str] = []

        def fake_detect() -> None:
            # Simulate detect discovering a pending migration.
            order.append("detect")

        svc, _ = make_service(tmp_path, detect_sort_change=fake_detect)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"

        # Track when is_save_sort_changed is consulted.
        orig_gate = svc._rom_info.is_save_sort_changed

        def wrapped_gate():
            order.append("gate")
            return orig_gate()

        svc._rom_info.is_save_sort_changed = wrapped_gate  # type: ignore[method-assign]

        await svc.pre_launch_sync(42)

        assert "detect" in order
        assert "gate" in order
        assert order.index("detect") < order.index("gate")


class TestPostExitSync:
    @pytest.mark.asyncio
    async def test_uploads_changed_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"new save data")

        result = await svc.post_exit_sync(42)
        assert result["success"] is True
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_disabled_skips(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.post_exit_sync(42)
        assert result["success"] is True
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_post_exit_disabled_in_settings(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.settings.sync_after_exit = False
        svc._save_sync_state.device_id = "test-device"

        result = await svc.post_exit_sync(42)
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_auto_registers_device(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # No device_id set
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")

        result = await svc.post_exit_sync(42)
        assert result["success"] is True
        assert svc._save_sync_state.device_id is not None

    # ------------------------------------------------------------------
    # Regression tests for issue #238 — detect-first invariant.
    #
    # Save-sync must refresh save-sort state via detect_sort_change
    # before computing saves_dir, so that Rule 1 / Rule 2 engage even
    # when a direct-Steam-launch race delivers post_exit_sync before
    # refreshMigrationState. See #238.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_post_exit_sync_invokes_detect_sort_change_before_sync(self, tmp_path):
        """detect_sort_change is called exactly once before the sync path runs (#238)."""
        call_order: list[str] = []

        def fake_detect() -> None:
            call_order.append("detect")

        svc, _ = make_service(tmp_path, detect_sort_change=fake_detect)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        # Patch _sync_rom_saves to record call ordering.
        orig_sync = svc._sync_engine._sync_rom_saves

        def wrapped_sync(rom_id):
            call_order.append("sync")
            return orig_sync(rom_id)

        svc._sync_engine._sync_rom_saves = wrapped_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is True
        # detect fired exactly once, before sync ran.
        assert call_order.count("detect") == 1
        assert call_order.index("detect") < call_order.index("sync")

    @pytest.mark.asyncio
    async def test_post_exit_sync_continues_when_detect_sort_change_raises(self, tmp_path, caplog):
        """If detect_sort_change raises, save-sync logs a warning and proceeds (#238)."""

        def boom() -> None:
            raise RuntimeError("cfg file unreadable")

        svc, _ = make_service(tmp_path, detect_sort_change=boom)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        with caplog.at_level(logging.WARNING, logger="test"):
            result = await svc.post_exit_sync(42)

        assert result["success"] is True
        # Sync still ran despite detect failure.
        assert result["synced"] == 1
        # Warning was logged.
        assert any("detect_sort_change failed" in rec.message for rec in caplog.records)

    @pytest.mark.asyncio
    async def test_post_exit_sync_works_when_detect_sort_change_is_none(self, tmp_path):
        """Default detect_sort_change=None: post_exit_sync still runs without error (#238)."""
        svc, _ = make_service(tmp_path)  # detect_sort_change not passed → None
        assert svc._sync_engine._detect_sort_change is None
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        result = await svc.post_exit_sync(42)

        assert result["success"] is True
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_post_exit_sync_message_includes_conflict_count(self, tmp_path):
        """post_exit_sync must surface conflict count in its message.

        Previously "Uploaded 0 save(s)" even with conflicts — user has no
        signal that sync is blocked on manual resolution.
        """
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id):
            return (0, [], [{"type": "newer_in_slot", "rom_id": rom_id}])

        svc._sync_engine._sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is True
        assert "1 conflict(s)" in result["message"]
        assert result["synced"] == 0


class TestCheckSaveStatusBackground:
    """Tests for the background save status check with event emit."""

    @pytest.mark.asyncio
    async def test_emits_save_status_updated(self, tmp_path):
        """Background check runs full status and emits result."""
        emitted = []

        async def fake_emit(event, *args):
            emitted.append((event, args))

        svc, _fake = make_service(tmp_path, emit=fake_emit)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        await svc.check_save_status_background(42)

        assert len(emitted) == 1
        assert emitted[0][0] == "save_status_updated"
        result = emitted[0][1][0]
        assert result["rom_id"] == 42
        assert len(result["files"]) >= 1

    @pytest.mark.asyncio
    async def test_no_emit_when_emit_is_none(self, tmp_path):
        """Background check works without emit (no crash)."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        # Should not raise even without emit
        await svc.check_save_status_background(42)

    @pytest.mark.asyncio
    async def test_swallows_errors(self, tmp_path):
        """Background check logs but does not raise on errors."""
        svc, fake = make_service(tmp_path)
        fake.fail_on_next(Exception("Server down"))

        # Should not raise
        await svc.check_save_status_background(42)


class TestUploadSpecialChars:
    """Upload with special characters (spaces, parentheses) in filename."""

    def test_find_saves_with_special_chars(self, tmp_path):
        svc, _ = make_service(tmp_path)
        rom_name = "Metroid - Zero Mission (USA)"
        file_name = f"{rom_name}.gba"
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name=file_name)
        _create_save(tmp_path, system="gba", rom_name=rom_name)

        result = svc._rom_info.find_save_files(42)

        assert len(result) == 1
        assert result[0]["filename"] == f"{rom_name}.srm"


class TestUpdateFileSyncState:
    """Tests for _update_file_sync_state."""

    def test_creates_proper_entry(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._sync_engine._update_file_sync_state("42", "pokemon.srm", server_resp, str(save_file), "gba")

        entry = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert entry.last_sync_hash == svc._save_file.checksum_md5(str(save_file))
        assert entry.last_sync_at is not None
        assert entry.last_sync_server_save_id == 200

    def test_creates_entry_with_new_fields(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._sync_engine._update_file_sync_state(
            "42",
            "pokemon.srm",
            server_resp,
            str(save_file),
            "gba",
            emulator_tag="retroarch-mgba",
            core_so="mgba_libretro",
        )

        game_state = svc._save_sync_state.saves["42"]
        assert game_state.emulator == "retroarch-mgba"
        assert game_state.last_synced_core == "mgba_libretro"
        assert game_state.active_slot == "default"

        file_state = game_state.files["pokemon.srm"]
        assert file_state.tracked_save_id == 200
        assert file_state.last_sync_server_save_id == 200

    def test_updates_emulator_on_existing_entry(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        # Pre-populate with old emulator tag
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {},
                "emulator": "retroarch",
                "system": "gba",
                "last_synced_core": None,
                "active_slot": "default",
            }
        )
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._sync_engine._update_file_sync_state(
            "42",
            "pokemon.srm",
            server_resp,
            str(save_file),
            "gba",
            emulator_tag="retroarch-mgba",
            core_so="mgba_libretro",
        )

        game_state = svc._save_sync_state.saves["42"]
        assert game_state.emulator == "retroarch-mgba"
        assert game_state.last_synced_core == "mgba_libretro"

    def test_core_so_none_does_not_overwrite(self, tmp_path):
        """core_so=None should not reset an already-set last_synced_core."""
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {},
                "emulator": "retroarch-mgba",
                "system": "gba",
                "last_synced_core": "mgba_libretro",
                "active_slot": "default",
            }
        )
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._sync_engine._update_file_sync_state(
            "42",
            "pokemon.srm",
            server_resp,
            str(save_file),
            "gba",
            emulator_tag="retroarch",
        )

        # last_synced_core unchanged because core_so=None
        game_state = svc._save_sync_state.saves["42"]
        assert game_state.last_synced_core == "mgba_libretro"

    def test_writes_last_sync_local_mtime_as_float(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"\x00" * 1024)
        local_path = str(save_file)
        server_response = _server_save()

        svc._sync_engine._update_file_sync_state("42", "pokemon.srm", server_response, local_path, "gba")

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert isinstance(file_state.last_sync_local_mtime, float)
        assert file_state.last_sync_local_mtime == pytest.approx(os.path.getmtime(local_path))

    def test_writes_last_sync_local_size_as_int(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"\x00" * 2048)
        local_path = str(save_file)
        server_response = _server_save()

        svc._sync_engine._update_file_sync_state("42", "pokemon.srm", server_response, local_path, "gba")

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert isinstance(file_state.last_sync_local_size, int)
        assert file_state.last_sync_local_size == 2048

    def test_does_not_write_old_local_mtime_at_last_sync_key(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path, system="gba", rom_name="pokemon")
        local_path = str(save_file)
        server_response = _server_save()

        svc._sync_engine._update_file_sync_state("42", "pokemon.srm", server_response, local_path, "gba")

        # The legacy field name was never part of FileSyncState; ensure the
        # serialised on-disk shape doesn't carry it either.
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert "local_mtime_at_last_sync" not in file_state.to_dict()

    def test_writes_none_for_missing_file(self, tmp_path):
        svc, _ = make_service(tmp_path)
        local_path = str(tmp_path / "saves" / "gba" / "missing.srm")
        server_response = _server_save()

        svc._sync_engine._update_file_sync_state("42", "missing.srm", server_response, local_path, "gba")

        file_state = svc._save_sync_state.saves["42"].files["missing.srm"]
        assert file_state.last_sync_local_mtime is None
        assert file_state.last_sync_local_size is None


class TestV47SyncFlow:
    def test_list_saves_passes_device_id(self, tmp_path):
        """v4.7: list_saves receives server_device_id."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "local-id"
        svc._save_sync_state.server_device_id = "server-dev-123"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        svc._sync_engine._sync_rom_saves(42)

        list_calls = [c for c in fake.call_log if c[0] == "list_saves"]
        assert len(list_calls) >= 1
        assert list_calls[0][2]["device_id"] == "server-dev-123"

    def test_upload_passes_device_id_and_slot(self, tmp_path):
        """v4.7: upload_save receives device_id and slot."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "local-id"
        svc._save_sync_state.server_device_id = "server-dev-123"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        svc._sync_engine._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        assert upload_calls[0][2]["device_id"] == "server-dev-123"
        assert upload_calls[0][2]["slot"] == "default"

    def test_v47_skip_when_is_current(self, tmp_path):
        """v4.7: server says is_current=True, local unchanged → skip."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        content = b"same content"
        _create_save(tmp_path, content=content)
        local_hash = hashlib.md5(content).hexdigest()

        # Pre-populate sync state (simulating previous sync)
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "last_sync_hash": local_hash,
                        "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                        "last_sync_server_save_id": 100,
                        "last_sync_server_size": len(content),
                    }
                }
            }
        )

        # Set up server save with device_syncs showing is_current=True
        fake.saves[100] = {
            "id": 100,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2026-02-17T06:00:00Z",
            "file_size_bytes": len(content),
            "device_syncs": [{"device_id": "dev-1", "is_current": True}],
        }

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)
        assert synced == 0
        assert errors == []
        assert conflicts == []

    def test_v47_download_when_not_current(self, tmp_path):
        """v4.7: server says is_current=False, local unchanged → download."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        content = b"old content"
        _create_save(tmp_path, content=content)
        local_hash = hashlib.md5(content).hexdigest()

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "last_sync_hash": local_hash,
                        "last_sync_server_updated_at": "2026-02-17T06:00:00Z",
                        "last_sync_server_save_id": 100,
                        "last_sync_server_size": len(content),
                    }
                }
            }
        )

        # Server has newer save, device is not current
        fake.saves[100] = {
            "id": 100,
            "rom_id": 42,
            "file_name": "pokemon.srm",
            "updated_at": "2026-02-17T08:00:00Z",
            "file_size_bytes": 2048,
            "device_syncs": [{"device_id": "dev-1", "is_current": False}],
        }

        synced, errors, _conflicts = svc._sync_engine._sync_rom_saves(42)
        assert synced == 1
        assert errors == []
        # Verify download happened
        assert 100 in fake.downloaded_files


class TestConfirmDownloadAfterSync:
    """Verify the device's last_synced_at is registered with RomM after each
    upload (PUT/POST) and download.

    is_current is computed server-side as
    ``device_save_sync.last_synced_at >= save.updated_at``. PUT/POST bump
    ``save.updated_at`` to NOW but do NOT touch the calling device's
    ``last_synced_at`` in every code path; we explicitly close that gap by
    calling ``confirm_download``. For downloads, the optimistic query-param on
    ``download_save_content`` upserts the row server-side before streaming.
    """

    def test_do_upload_save_post_calls_confirm_download(self, tmp_path):
        """POST (no save_id) → confirm_download fires for the new save_id."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        svc._sync_engine._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # FakeSaveApi mints a new save_id starting from 1000 on POST
        new_save_id = next(iter(fake.saves.values()))["id"]

        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert len(confirm_calls) == 1
        assert confirm_calls[0][1] == (new_save_id, "dev-1")

    def test_do_upload_save_put_calls_confirm_download(self, tmp_path):
        """PUT (existing save_id) → confirm_download fires for that save_id."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # Pre-existing tracked server save
        fake.saves[100] = _server_save(save_id=100, rom_id=42)
        server_save = fake.saves[100]

        svc._sync_engine._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba", server_save=server_save)

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT path: save_id kwarg passed to upload_save
        assert upload_calls[0][2]["save_id"] == 100

        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert len(confirm_calls) == 1
        assert confirm_calls[0][1] == (100, "dev-1")

    def test_do_upload_save_skips_confirm_when_no_device_id(self, tmp_path):
        """No registered device → confirm_download is not called (no-op)."""
        svc, fake = make_service(tmp_path)
        # server_device_id stays None — device not registered
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        svc._sync_engine._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")

        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert confirm_calls == []

    def test_do_upload_save_swallows_confirm_download_error(self, tmp_path):
        """confirm_download failure must NOT bubble — upload is reported successful."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.server_device_id = "dev-1"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # Patch confirm_download to raise; the upload itself must still complete.
        original_confirm = fake.confirm_download

        def boom(save_id: int, device_id: str) -> dict:
            fake.call_log.append(("confirm_download", (save_id, device_id), {}))
            raise RommApiError("HTTP 500: Server Error", url="/api/saves/x/downloaded", method="POST")

        fake.confirm_download = boom  # type: ignore[method-assign]
        try:
            result = svc._sync_engine._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba")
        finally:
            fake.confirm_download = original_confirm  # type: ignore[method-assign]

        # Upload completed, returned a result with id, AND the file_state was updated.
        assert result.get("id") is not None
        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert len(confirm_calls) == 1
        # File state still recorded the upload (not blocked by confirm failure)
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id is not None

    def test_do_download_save_passes_device_id_and_optimistic(self, tmp_path):
        """download_save_content must pass device_id + optimistic=True so the
        server upserts our DeviceSaveSync row before streaming. This makes a
        follow-up confirm_download unnecessary for the download path.
        """
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.server_device_id = "dev-1"
        saves_dir = str(tmp_path / "saves" / "gba")
        os.makedirs(saves_dir, exist_ok=True)
        server_save = _server_save(save_id=99)

        svc._sync_engine._do_download_save(server_save, saves_dir, "pokemon.srm", "42", "gba")

        dl_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(dl_calls) == 1
        kwargs = dl_calls[0][2]
        assert kwargs["device_id"] == "dev-1"
        assert kwargs["optimistic"] is True


class TestTrackedSaveIdMatching:
    """Tests that sync uses tracked_save_id to match server saves instead of filename."""

    def test_timestamp_server_save_not_treated_as_separate_download(self, tmp_path):
        """Server save matched by tracked_save_id should not appear as server-only download."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)
        local_hash = _file_md5(str(save_path))

        fake.saves[42] = {
            "id": 42,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-24_15-18-50].srm",
            "updated_at": "2026-03-20T10:00:00",
            "file_size_bytes": 1024,
            "emulator": "retroarch",
            "download_path": "/saves/pokemon [2026-03-24_15-18-50].srm",
        }

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "slot_confirmed": True,
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 42,
                        "last_sync_hash": local_hash,
                        "last_sync_at": "2026-03-20T10:00:00",
                        "last_sync_server_updated_at": "2026-03-20T10:00:00",
                        "last_sync_server_save_id": 42,
                        "last_sync_server_size": 1024,
                        "local_mtime_at_last_sync": "2026-03-20T10:00:00",
                    },
                },
            }
        )

        # Sync should NOT download the timestamp-named file as a new server-only save
        _synced, errors, _conflicts = svc._sync_engine._sync_rom_saves(42)
        assert len(errors) == 0
        # No downloads should have occurred (files are in sync)
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0

    @pytest.mark.asyncio
    async def test_get_save_status_uses_tracked_save_id(self, tmp_path):
        """get_save_status should not show timestamp-named server save as separate file."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        fake.saves[42] = {
            "id": 42,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-24_15-18-50].srm",
            "updated_at": "2026-03-20T10:00:00",
            "file_size_bytes": 1024,
            "emulator": "retroarch",
            "download_path": "/saves/pokemon [2026-03-24_15-18-50].srm",
        }

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "slot_confirmed": True,
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 42,
                        "last_sync_hash": hashlib.md5(b"\x00" * 1024).hexdigest(),
                        "last_sync_at": "2026-03-20T10:00:00",
                        "last_sync_server_updated_at": "2026-03-20T10:00:00",
                        "last_sync_server_save_id": 42,
                        "last_sync_server_size": 1024,
                        "local_mtime_at_last_sync": "2026-03-20T10:00:00",
                    },
                },
            }
        )

        result = await svc.get_save_status(42)
        filenames = [f["filename"] for f in result["files"]]
        # The timestamp-named server save should NOT appear as a separate file
        assert "pokemon [2026-03-24_15-18-50].srm" not in filenames
        # The local filename should appear
        assert "pokemon.srm" in filenames

    @pytest.mark.asyncio
    async def test_status_fallback_matches_newest_no_phantom_downloads(self, tmp_path):
        """Status with no tracked_save_id matches newest server save, no phantom downloads."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        fake.saves[10] = {
            "id": 10,
            "rom_id": 42,
            "file_name": "pokemon [old].srm",
            "updated_at": "2026-03-24T10:00:00",
            "file_size_bytes": 100,
            "emulator": "retroarch",
            "download_path": "/saves/pokemon [old].srm",
            "slot": "default",
        }
        fake.saves[20] = {
            "id": 20,
            "rom_id": 42,
            "file_name": "pokemon [new].srm",
            "updated_at": "2026-03-24T15:00:00",
            "file_size_bytes": 200,
            "emulator": "retroarch",
            "download_path": "/saves/pokemon [new].srm",
            "slot": "default",
        }

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "slot_confirmed": True,
                "files": {},
            }
        )

        result = await svc.get_save_status(42)
        filenames = [f["filename"] for f in result["files"]]

        # The local file should appear (matched to newest server save)
        assert "pokemon.srm" in filenames
        # Timestamp server files should NOT appear as separate entries
        assert "pokemon [old].srm" not in filenames
        assert "pokemon [new].srm" not in filenames

    def test_server_only_downloads_newest_with_local_filename(self, tmp_path):
        """Case 2: no local file, server has multiple timestamped saves.
        Should download only the newest, saved as the correct local filename."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        # NO local save created — Case 2

        # Server has 3 timestamped versions of the same save
        for sid, ts in [(16, "15-18-50"), (17, "15-19-15"), (18, "15-19-26")]:
            fake.saves[sid] = {
                "id": sid,
                "rom_id": 42,
                "file_name": f"pokemon [2026-03-24_{ts}].srm",
                "file_name_no_tags": "pokemon",
                "file_extension": "srm",
                "updated_at": f"2026-03-24T{ts.replace('-', ':')}",
                "file_size_bytes": 1024,
                "emulator": "retroarch-mgba",
                "slot": "default",
                "download_path": f"/saves/pokemon [2026-03-24_{ts}].srm",
            }

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "slot_confirmed": True,
                "files": {},
            }
        )

        synced, errors, _conflicts = svc._sync_engine._sync_rom_saves(42)
        assert len(errors) == 0
        assert synced == 1  # only ONE download

        # Should download only once (the newest, id=18)
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 1
        assert download_calls[0][1][0] == 18  # save_id=18 (newest)

        # File should be saved as pokemon.srm (local name), NOT timestamp name
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()
        assert not (saves_dir / "pokemon [2026-03-24_15-19-26].srm").exists()

    @pytest.mark.asyncio
    async def test_status_server_only_shows_local_filename(self, tmp_path):
        """Status display should show local filename for server-only saves, not timestamp."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        # NO local save

        fake.saves[18] = {
            "id": 18,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-24_15-19-26].srm",
            "file_name_no_tags": "pokemon",
            "file_extension": "srm",
            "updated_at": "2026-03-24T15:19:26",
            "file_size_bytes": 1024,
            "emulator": "retroarch-mgba",
            "slot": "default",
            "download_path": "/saves/pokemon [2026-03-24_15-19-26].srm",
        }

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "slot_confirmed": True,
                "files": {},
            }
        )

        result = await svc.get_save_status(42)
        filenames = [f["filename"] for f in result["files"]]
        assert "pokemon.srm" in filenames
        assert "pokemon [2026-03-24_15-19-26].srm" not in filenames


class TestOlderVersionSkipping:
    """Older stacked versions in the same slot must not be downloaded."""

    def test_different_slot_filtered_out(self, tmp_path):
        """Saves in a different slot should be filtered out entirely."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "dev-1"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local save")

        # Matched in slot=default
        fake.saves[10] = _server_save(
            save_id=10,
            filename="pokemon.srm",
            updated_at="2026-03-24T15:00:00",
            slot="default",
        )
        # Unmatched in slot=portable — filtered out by active_slot
        fake.saves[20] = _server_save(
            save_id=20,
            filename="pokemon [old].srm",
            updated_at="2026-03-20T10:00:00",
            slot="portable",
        )

        local_hash = _file_md5(tmp_path / "saves" / "gba" / "pokemon.srm")
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "slot_confirmed": True,
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 10,
                        "last_sync_hash": local_hash,
                        "last_sync_at": "2026-03-24T15:00:00",
                        "last_sync_server_updated_at": "2026-03-24T15:00:00",
                        "last_sync_server_save_id": 10,
                        "last_sync_server_size": 1024,
                        "local_mtime_at_last_sync": "2026-03-24T15:00:00",
                    },
                },
            }
        )

        _synced, _errors, _conflicts = svc._sync_engine._sync_rom_saves(42)
        # pokemon [old].srm in slot=portable is filtered out — no download
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 0


class TestOwnUploadIds:
    """Tests for own_upload_ids tracking and the uploaded_by_us flag."""

    @pytest.mark.asyncio
    async def test_post_upload_appends_own_upload_id(self, tmp_path):
        """After a POST upload (new save), the returned save_id is added to own_upload_ids."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        # No pre-existing server save — this will be a POST (save_id=None)
        await svc.sync_rom_saves(42)

        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        returned_id = upload_calls[0][2]["save_id"]  # save_id kwarg from upload_save call
        # The save_id passed to upload_save should be None (POST path)
        assert returned_id is None

        rom_state = svc._save_sync_state.saves["42"]
        own_ids = rom_state.own_upload_ids or []
        assert len(own_ids) == 1
        # The id in the list must match what fake returned
        new_save_id = next(iter(fake.saves.values()))["id"]
        assert new_save_id in own_ids

    @pytest.mark.asyncio
    async def test_post_upload_idempotent_in_own_list(self, tmp_path):
        """Calling _do_upload_save twice with the same resulting save_id does not duplicate."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_file = _create_save(tmp_path)

        # Pre-populate own_upload_ids with the id that fake will return (1000)
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {},
                "system": "gba",
                "active_slot": "default",
                "own_upload_ids": [1000],
            }
        )
        # Fake will return the same id=1000 because filename matches existing
        fake.saves[1000] = _server_save(save_id=1000, rom_id=42)

        # Call internal upload with no server_save (POST path)
        svc._sync_engine._do_upload_save(42, str(save_file), "pokemon.srm", "42", "gba", server_save=None)

        rom_state = svc._save_sync_state.saves["42"]
        assert rom_state.own_upload_ids is not None
        # Should still have exactly one entry for that id
        assert rom_state.own_upload_ids.count(1000) == 1

    @pytest.mark.asyncio
    async def test_put_upload_does_not_touch_own_list(self, tmp_path):
        """Updating an existing tracked save (PUT path) does not modify own_upload_ids."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        save_file = _create_save(tmp_path)

        # Pre-existing server save (id=100) — upload_save called with save_id=100 → PUT
        fake.saves[100] = _server_save(save_id=100, rom_id=42)
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {"pokemon.srm": {"tracked_save_id": 100, "last_sync_hash": "old"}},
                "system": "gba",
                "active_slot": "default",
                "own_upload_ids": [99],  # pre-existing unrelated id
            }
        )

        server_save = fake.saves[100]
        svc._sync_engine._do_upload_save(42, str(save_file), "pokemon.srm", "42", "gba", server_save=server_save)

        rom_state = svc._save_sync_state.saves["42"]
        # own_upload_ids must not have changed (100 not added, 99 still there)
        assert rom_state.own_upload_ids == [99]

    @pytest.mark.asyncio
    async def test_get_save_status_legacy_rom_state_returns_none(self, tmp_path):
        """When rom state exists but own_upload_ids key is absent, uploaded_by_us is None."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True

        fake.saves[26] = _server_save(save_id=26, rom_id=42, filename="pokemon.srm")

        # Legacy state: own_upload_ids key is absent
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {},
                "system": "gba",
                "active_slot": None,
                # no own_upload_ids key
            }
        )

        result = await svc.get_save_status(42)

        files_by_id = {f["server_save_id"]: f for f in result["files"] if f.get("server_save_id")}
        assert files_by_id[26]["uploaded_by_us"] is None

    @pytest.mark.asyncio
    async def test_rollback_to_foreign_version_preserves_own_upload_ids(self, tmp_path):
        """Rolling back to a foreign save (not in own_upload_ids) does not modify own_upload_ids."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        save_file = _create_save(tmp_path)
        local_hash = _file_md5(str(save_file))

        # own save is 26, tracked is 26 (clean state)
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 26,
                        "last_sync_hash": local_hash,
                    }
                },
                "system": "gba",
                "active_slot": "default",
                "own_upload_ids": [26],
            }
        )
        fake.saves[26] = _server_save(save_id=26, rom_id=42, slot="default")
        # Foreign older version to roll back to
        fake.saves[27] = _server_save(save_id=27, rom_id=42, slot="default", updated_at="2026-01-01T00:00:00Z")

        result = await svc.rollback_to_version(42, "default", 27)

        assert result["status"] == "ok"
        # own_upload_ids must be unchanged — 27 was not POSTed by us
        rom_state = svc._save_sync_state.saves["42"]
        assert rom_state.own_upload_ids == [26]


class TestPromoteLocalSlotPersistsState:
    """Regression for #346.

    The PUT-path edge case from the issue: server save tracked but the slot
    marker is still ``'local'`` (stale). On promotion, the in-memory mutation
    must reach disk so the next plugin start sees ``source='server'``.
    """

    def test_put_path_promotion_survives_reload(self, tmp_path):
        """A PUT upload that promotes a stale local-slot marker persists to disk."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="dev-1")
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path)

        # Pre-existing tracked server save → upload_save called with save_id=100 (PUT path).
        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default")
        server_save = fake.saves[100]

        # rom_state has the slot still flagged 'local' (stale marker).
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {"pokemon.srm": {"tracked_save_id": 100, "last_sync_hash": "old"}},
                "system": "gba",
                "active_slot": "default",
                "slots": {"default": {"source": "local", "count": 1}},
            }
        )

        svc._sync_engine._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba", server_save=server_save)

        in_mem = svc._save_sync_state.saves["42"].slots["default"]
        assert in_mem["source"] == "server"
        assert in_mem["count"] == 1

        # A fresh service reading from the same on-disk state sees the promotion —
        # the in-memory mutation reached disk.
        reloaded_svc, _ = make_service(tmp_path)
        reloaded_svc.load_state()
        reloaded = reloaded_svc._save_sync_state.saves["42"].slots["default"]
        assert reloaded["source"] == "server"
        assert reloaded["count"] == 1


class TestDoUploadSaveFileStatePersistence:
    """Regression for #409.

    The PUT branch with a slot already marked ``source='server'`` is a no-op
    for slot promotion. Without an unconditional persist at the end of
    ``_do_upload_save``, the per-file ``last_sync_hash`` / ``tracked_save_id``
    written by ``_update_file_sync_state`` never reaches disk on that path —
    so after a plugin restart the next sync re-detects drift and re-uploads
    the same content. This test asserts the upload outcome is persisted
    regardless of which slot-promotion branch fired.
    """

    def test_put_path_persists_file_sync_state_when_slot_already_server(self, tmp_path):
        """PUT with slot.source='server' (no promotion) still persists file sync state."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="dev-1")
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"freshly-edited save")
        expected_hash = _file_md5(str(save_path))

        # Pre-existing tracked server save → upload_save called with save_id=100 (PUT path).
        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default")
        server_save = fake.saves[100]

        # Slot already known-server: _promote_local_slot_to_server is a no-op
        # on this branch, so the file-state writes have no incidental persist
        # to ride on. File state holds a stale baseline hash to make the
        # regression visible — after the upload, the on-disk hash must be the
        # current local hash (not the stale one).
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {"pokemon.srm": {"tracked_save_id": 100, "last_sync_hash": "stale-pre-upload"}},
                "system": "gba",
                "active_slot": "default",
                "slots": {"default": {"source": "server", "count": 1}},
            }
        )

        svc._sync_engine._do_upload_save(42, str(save_path), "pokemon.srm", "42", "gba", server_save=server_save)

        # In-memory state captured the fresh hash.
        in_mem_file = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert in_mem_file.last_sync_hash == expected_hash
        assert in_mem_file.tracked_save_id == 100

        # A fresh service reading from the same on-disk state must see the
        # fresh hash — without it, the next sync re-detects drift and uploads
        # the same content again (#409 leak).
        reloaded_svc, _ = make_service(tmp_path)
        reloaded_svc.load_state()
        reloaded_file = reloaded_svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert reloaded_file.last_sync_hash == expected_hash
        assert reloaded_file.tracked_save_id == 100


class TestSyncRomSavesDispatch:
    def test_sync_rom_saves_skip_when_synced(self, tmp_path):
        """is_current=true + matching hash + tracked → Skip, no I/O."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"pristine save")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": local_hash,
                        "last_sync_server_updated_at": ss["updated_at"],
                    }
                }
            }
        )

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 0
        assert errors == []
        assert conflicts == []
        # No upload/download initiated.
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)

    def test_sync_rom_saves_upload_post_when_no_server_save(self, tmp_path):
        """No server saves in slot but local exists → Upload (POST)."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"new local")

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # POST → save_id is None
        assert upload_calls[0][2]["save_id"] is None

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id is not None
        assert file_state.last_sync_hash

    def test_sync_rom_saves_download_when_server_changed(self, tmp_path):
        """is_current=false + local hash matches last_sync_hash → Download."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"unchanged local")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": local_hash,
                        "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                    }
                }
            }
        )

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        # Download_save_content was called against the server save id.
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 1
        assert download_calls[0][1][0] == 100

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id == 100
        assert file_state.last_sync_hash  # updated to downloaded content's hash

    def test_sync_rom_saves_conflict_when_both_changed(self, tmp_path):
        """is_current=false + local hash diverges → Conflict, no I/O."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"diverged local")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "deadbeef" * 4,  # baseline differs from current local
                        "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                    }
                }
            }
        )

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 0
        assert errors == []
        assert len(conflicts) == 1
        c = conflicts[0]
        assert isinstance(c, dict)
        assert c["type"] == "sync_conflict"
        assert c["rom_id"] == 42
        assert c["filename"] == "pokemon.srm"
        assert c["server_save_id"] == 100
        assert c["server_updated_at"] == ss["updated_at"]
        assert c["server_size"] == ss["file_size_bytes"]
        assert c["local_path"] == str(save_path)
        assert c["local_hash"] == local_hash
        assert c["local_mtime"] is not None
        assert c["local_size"] == os.path.getsize(str(save_path))
        assert "created_at" in c

    def test_sync_rom_saves_server_only_downloads(self, tmp_path):
        """No local file, one server save in slot → Download."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()

    def test_sync_rom_saves_upload_put_when_local_diverged(self, tmp_path):
        """is_current=true + local hash diverges from baseline → Upload (PUT)
        against the existing tracked save id."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"diverged offline")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "0" * 32,  # baseline differs from current local
                        "last_sync_server_updated_at": ss["updated_at"],
                    }
                }
            }
        )

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT — save_id is the existing server save id
        assert upload_calls[0][2]["save_id"] == 100

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.last_sync_hash == local_hash

    def test_sync_rom_saves_skip_with_adopt_baseline_writes_hash(self, tmp_path):
        """is_current=true + local present + no baseline → Skip + adopt_baseline:
        no I/O but state.last_sync_hash gets recorded as local_hash."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"first sync")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        # No file_state at all — no baseline yet.
        svc._save_sync_state.saves["42"] = RomSaveState()

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 0
        assert errors == []
        assert conflicts == []
        # No I/O initiated.
        assert not any(c[0] in ("upload_save", "download_save_content", "download_save") for c in fake.call_log)
        # Baseline now persisted.
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.last_sync_hash == local_hash

    def test_sync_rom_saves_recovery_download_when_no_local(self, tmp_path):
        """is_current=true on the picked save but local file is gone → Download
        to recover the canonical content."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # No _create_save here — local file is absent.

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "abc",
                        "last_sync_server_updated_at": ss["updated_at"],
                    }
                }
            }
        )

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 1
        assert download_calls[0][1][0] == 100
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()

    def test_dispatch_upload_put_targets_correct_save(self, tmp_path):
        """Dispatcher PUT: target_save_id selects the right server save from
        the candidate list and uploads against it."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-edit")

        ss = _server_save_with_syncs(
            save_id=100,
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        # Build a state where compute_sync_action emits Upload(target_save_id=100)
        # via the is_current=true + diverged hash branch.
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "0" * 32,
                        "last_sync_server_updated_at": ss["updated_at"],
                    }
                }
            }
        )

        synced, errors, conflicts = svc._sync_engine._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT — saved against the server save id provided by the algorithm.
        assert upload_calls[0][2]["save_id"] == 100
        # Local was not lost.
        assert save_path.read_bytes() == b"local-edit"

    def test_sync_rom_saves_persists_last_sync_check_at(self, tmp_path):
        """Every sync run records last_sync_check_at on the rom-level entry."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # Pure no-op: no local, no server saves.

        before_entry = svc._save_sync_state.saves.get("42")
        assert before_entry is None or before_entry.last_sync_check_at is None

        svc._sync_engine._sync_rom_saves(42)

        after = svc._save_sync_state.saves["42"].last_sync_check_at
        assert after is not None and isinstance(after, str)


class TestResolveSyncConflict:
    @pytest.mark.asyncio
    async def test_resolve_keep_local_hash_match_short_circuits(self, tmp_path):
        """Local hash matches server's content hash → no PUT, state updated."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"identical content")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        # Make the server hash equal to the local hash by uploading the same
        # file as the source: FakeSaveApi.download_save copies uploaded_files.
        fake.saves[100] = ss
        fake.uploaded_files[100] = str(save_path)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is True
        assert result["action"] == "keep_local"
        assert not any(c[0] == "upload_save" for c in fake.call_log)

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id == 100
        assert file_state.last_sync_hash == local_hash

    @pytest.mark.asyncio
    async def test_resolve_keep_local_hash_mismatch_uploads_put(self, tmp_path):
        """Local differs from server content → PUT against existing save id."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-edited")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        # Server has different content uploaded — hash will not match.
        other = tmp_path / "other.bin"
        other.write_bytes(b"server-flavor")
        fake.saves[100] = ss
        fake.uploaded_files[100] = str(other)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is True
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT — save_id was passed
        assert upload_calls[0][2]["save_id"] == 100

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.last_sync_hash == local_hash

    @pytest.mark.asyncio
    async def test_resolve_keep_local_falls_back_when_server_hash_fetch_raises(self, tmp_path):
        """When ``_get_server_save_hash`` raises out of retry (retries exhausted),
        the keep_local resolver swallows it and treats ``server_hash`` as ``None``.

        That sinks the adopt-without-upload short-circuit (which requires
        ``server_hash`` truthy and equal to ``local_hash``) and the PUT path
        runs unconditionally. Degraded behaviour, but no failure surfaced to
        the user.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-edited")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss
        # No uploaded_files entry — wouldn't matter anyway, the download_save
        # call inside _get_server_save_hash is going to raise before reading.

        # Make ``_get_server_save_hash`` re-raise: ``download_save`` raises
        # and the retry mock reports the exception as retryable, so the
        # inner ``except Exception`` in ``_get_server_save_hash`` re-raises
        # and the outer ``except Exception`` in
        # ``_resolve_conflict_keep_local`` catches it. We monkey-patch
        # ``download_save`` directly (rather than ``fail_on_next``, which
        # would consume on the earlier ``list_saves`` call).
        def _raise_on_download(save_id: int, dest_path: str) -> None:
            fake.call_log.append(("download_save", (save_id, dest_path), {}))
            raise RommApiError("transient")

        fake.download_save = _raise_on_download  # type: ignore[method-assign]
        svc._sync_engine._retry.is_retryable.return_value = True  # type: ignore[attr-defined]

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is True
        assert result["action"] == "keep_local"
        # The hash-match short-circuit MUST NOT have fired — its branch
        # records ``tracked_save_id`` without an upload. We instead expect
        # the PUT upload path to have run.
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT — save_id was passed (existing save id, not None).
        assert upload_calls[0][2]["save_id"] == 100
        # download_save was attempted exactly once (the one that raised).
        download_calls = [c for c in fake.call_log if c[0] == "download_save"]
        assert len(download_calls) == 1

        # State carries the local hash from the successful PUT.
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.last_sync_hash == local_hash

    @pytest.mark.asyncio
    async def test_resolve_use_server_downloads_and_persists(self, tmp_path):
        """use_server downloads server, overwrites local, updates state."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-stale")

        # Server has different content
        server_content = tmp_path / "server-content.bin"
        server_content.write_bytes(b"server-truth")
        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss
        fake.uploaded_files[100] = str(server_content)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="use_server",
        )

        assert result["success"] is True
        # Local file overwritten with server content
        assert save_path.read_bytes() == b"server-truth"
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id == 100
        assert file_state.last_sync_hash == _file_md5(str(save_path))

    @pytest.mark.asyncio
    async def test_resolve_invalid_action_returns_error(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="foo",
        )

        assert result["success"] is False
        assert "invalid" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_resolve_rom_not_installed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)

        result = await svc.resolve_sync_conflict(
            rom_id=999,
            filename="pokemon.srm",
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is False
        assert result["message"]

    @pytest.mark.asyncio
    async def test_resolve_server_fetch_failure(self, tmp_path):
        """When list_saves raises, return failure without mutating state."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"x")

        # Pre-populate state to assert it stays untouched.
        original_state = RomSaveState(
            files={"pokemon.srm": FileSyncState(tracked_save_id=100, last_sync_hash="abc")},
        )
        svc._save_sync_state.saves["42"] = original_state

        fake.fail_on_next(RommApiError("network"))

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is False
        assert "Failed to fetch saves" in result["message"]
        # State left as-is — no mutation
        assert svc._save_sync_state.saves["42"] == original_state

    @pytest.mark.asyncio
    async def test_resolve_no_server_saves_in_slot(self, tmp_path):
        """Empty slot post-fetch returns success=False with a clear message.

        Implementation note: ``resolve_sync_conflict`` reaches the slot-empty
        branch via ``_filter_server_saves_to_slot`` and returns
        ``{"success": False, "message": "No server save in active slot"}``.
        """
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is False
        assert "no server save" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_resolve_filename_symmetry_keep_local_uses_canonical_target(self, tmp_path):
        """keep_local and use_server must resolve the on-disk path the same way.

        Both branches must derive ``<rom_name>.<server.file_extension>`` from
        the server save, ignoring the frontend-supplied ``filename`` for I/O.
        Otherwise an extension drift between the frontend label and the
        canonical name produces divergent disk and state outcomes for the
        same conflict.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # Canonical local save: pokemon.srm (matches server.file_extension).
        canonical_path = _create_save(tmp_path, content=b"local-progress")
        canonical_hash = _file_md5(str(canonical_path))

        # Server save advertises file_extension=srm; frontend will send a
        # mismatched filename (pokemon.sav). Server hash differs so the
        # adopt-without-upload short-circuit doesn't fire.
        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        ss["file_extension"] = "srm"
        other = tmp_path / "server-bytes.bin"
        other.write_bytes(b"server-flavor")
        fake.saves[100] = ss
        fake.uploaded_files[100] = str(other)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.sav",  # diverges from server canonical
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is True
        # Canonical local file remained — no rename, no orphan at the
        # frontend-supplied name.
        assert canonical_path.read_bytes() == b"local-progress"
        assert not (tmp_path / "saves" / "gba" / "pokemon.sav").exists()
        # Upload PUT used the canonical filename, not the user-supplied one.
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        upload_path = upload_calls[0][1][1]
        assert os.path.basename(upload_path) == "pokemon.srm"
        # State keyed by canonical filename — never by the frontend label.
        files_state = svc._save_sync_state.saves["42"].files
        assert "pokemon.srm" in files_state
        assert "pokemon.sav" not in files_state
        assert files_state["pokemon.srm"].last_sync_hash == canonical_hash

    @pytest.mark.asyncio
    async def test_resolve_filename_symmetry_use_server_keys_state_on_canonical(self, tmp_path):
        """use_server with a mismatched frontend filename still writes at the canonical path.

        Pairs with ``test_resolve_filename_symmetry_keep_local_uses_canonical_target``
        to assert both branches converge on the same end state regardless of
        the frontend label.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local-stale")

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        ss["file_extension"] = "srm"
        server_bytes = tmp_path / "server-content.bin"
        server_bytes.write_bytes(b"server-truth")
        fake.saves[100] = ss
        fake.uploaded_files[100] = str(server_bytes)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.sav",  # frontend label diverges from canonical
            server_save_id=100,
            action="use_server",
        )

        assert result["success"] is True
        # Download landed at the canonical path, not the frontend label.
        canonical_path = tmp_path / "saves" / "gba" / "pokemon.srm"
        assert canonical_path.read_bytes() == b"server-truth"
        assert not (tmp_path / "saves" / "gba" / "pokemon.sav").exists()
        files_state = svc._save_sync_state.saves["42"].files
        assert "pokemon.srm" in files_state
        assert "pokemon.sav" not in files_state
        assert files_state["pokemon.srm"].tracked_save_id == 100

    @pytest.mark.asyncio
    async def test_resolve_keep_local_raises_when_canonical_path_missing(self, tmp_path):
        """If the local file is not at the canonical path, keep_local raises.

        Defensive companion to the symmetry fix: we never silently rename
        across extensions to satisfy a frontend label. A file named
        ``pokemon.sav`` on disk while the server save's canonical target is
        ``pokemon.srm`` must surface as ``FileNotFoundError`` so the user
        can rectify the mismatch instead of having two divergent files
        appear from a successful-looking resolve.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # Local file only at the non-canonical path.
        saves_dir = tmp_path / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)
        (saves_dir / "pokemon.sav").write_bytes(b"local-noncanonical")

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        ss["file_extension"] = "srm"
        fake.saves[100] = ss

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.sav",
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is False
        assert "not found" in result["message"].lower()
        # No upload was attempted.
        assert not any(c[0] == "upload_save" for c in fake.call_log)


class TestResolveSyncConflictStaleConflict:
    """Round-trip validation of ``server_save_id`` guards against a third-device
    race: another device PUTs into the slot while the conflict modal is open,
    and the client's stale id should not be silently rewritten."""

    @pytest.mark.asyncio
    async def test_resolve_keep_local_rejects_when_server_head_advanced(self, tmp_path):
        """Client passes server_save_id=100; server head is id=200 → stale_conflict.

        Asserts the dangerous PUT never fires and state stays untouched.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local-edited")

        # The id=100 save the modal was opened against is no longer the head:
        # a third device uploaded id=200 with a newer updated_at into the slot.
        newer_server = _server_save_with_syncs(
            save_id=200,
            updated_at="2026-01-02T00:00:00Z",
            device_syncs=[{"device_id": "device-2", "is_current": True}],
        )
        fake.saves[200] = newer_server

        # Snapshot state so the no-mutation assertion is exact.
        state_before = svc._save_sync_state.to_dict()

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is False
        assert result["error_code"] == "stale_conflict"
        assert result["message"]
        # No PUT/POST fired against the head save — the whole point of the guard.
        assert not any(c[0] == "upload_save" for c in fake.call_log)
        # State unchanged.
        assert svc._save_sync_state.to_dict() == state_before

    @pytest.mark.asyncio
    async def test_resolve_use_server_rejects_when_server_head_advanced(self, tmp_path):
        """use_server with a stale server_save_id is also rejected — the user
        chose to download id=100, not id=200; surfacing id=200 silently would
        also be a silent overwrite of the user's intent."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-stale")

        newer_server = _server_save_with_syncs(
            save_id=200,
            updated_at="2026-01-02T00:00:00Z",
            device_syncs=[{"device_id": "device-2", "is_current": True}],
        )
        # Make the server's "newer" save downloadable so we can prove the
        # download never fired.
        server_bytes = tmp_path / "third-device-bytes.bin"
        server_bytes.write_bytes(b"third-device-content")
        fake.saves[200] = newer_server
        fake.uploaded_files[200] = str(server_bytes)

        # Snapshot state so the no-mutation assertion is exact.
        state_before = svc._save_sync_state.to_dict()

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="use_server",
        )

        assert result["success"] is False
        assert result["error_code"] == "stale_conflict"
        # Local file untouched — no silent download of the wrong server save.
        assert save_path.read_bytes() == b"local-stale"
        # State unchanged.
        assert svc._save_sync_state.to_dict() == state_before

    @pytest.mark.asyncio
    async def test_resolve_succeeds_when_server_head_matches(self, tmp_path):
        """Same flow but id=100 is still the head — resolution proceeds normally."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-edited")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            save_id=100,
            updated_at="2026-01-01T00:00:00Z",
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        other = tmp_path / "server-bytes.bin"
        other.write_bytes(b"server-flavor")
        fake.saves[100] = ss
        fake.uploaded_files[100] = str(other)

        result = await svc.resolve_sync_conflict(
            rom_id=42,
            filename="pokemon.srm",
            server_save_id=100,
            action="keep_local",
        )

        assert result["success"] is True
        assert result["action"] == "keep_local"
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        assert upload_calls[0][2]["save_id"] == 100
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.last_sync_hash == local_hash


class TestMigrationPendingGuards:
    """The defense-in-depth migration-pending guards in pre_launch_sync and
    post_exit_sync. The decorator on the public callable is the primary gate;
    this in-engine guard catches a future caller that bypasses it (engine.py
    lines 286-292 / 340-347)."""

    @pytest.mark.asyncio
    async def test_pre_launch_sync_returns_blocked_when_migration_pending(self, tmp_path):
        """pre_launch_sync must short-circuit with blocked_by_migration=True."""
        svc, fake = make_service(
            tmp_path,
            is_retrodeck_migration_pending=lambda: True,
        )
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"unsyncable")

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert result["blocked_by_migration"] is True
        assert result["synced"] == 0
        # No upload/download initiated — the guard fired before sync ran.
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_post_exit_sync_returns_blocked_when_migration_pending(self, tmp_path):
        """post_exit_sync must short-circuit with blocked_by_migration=True."""
        svc, fake = make_service(
            tmp_path,
            is_retrodeck_migration_pending=lambda: True,
        )
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"unsyncable")

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result["blocked_by_migration"] is True
        assert result["synced"] == 0
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)


class TestEnsureDeviceRegisteredFailurePaths:
    """When register_device fails, the four sync callables must surface
    DEVICE_NOT_REGISTERED instead of proceeding with a missing device_id
    (engine.py lines 309-311 / 365 / 407 / 437-439)."""

    @pytest.mark.asyncio
    async def test_pre_launch_sync_returns_device_not_registered_on_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # No device_id set — triggers ensure_device_registered.
        _install_rom(svc, tmp_path)
        # register_device raises → ensure_device_registered returns success=False.
        fake.fail_on_next(RommApiError("Server unreachable"))

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert "Device" in result["message"] or "device" in result["message"]
        # No sync ran — the guard returned early.
        assert not any(c[0] == "list_saves" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_post_exit_sync_returns_device_not_registered_on_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # No device_id set.
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")
        fake.fail_on_next(RommApiError("Server unreachable"))

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert "Device" in result["message"] or "device" in result["message"]
        # No upload ran.
        assert not any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_rom_saves_returns_device_not_registered_on_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # No device_id set.
        _install_rom(svc, tmp_path)
        fake.fail_on_next(RommApiError("Server unreachable"))

        result = await svc.sync_rom_saves(42)

        assert result["success"] is False
        assert "Device" in result["message"] or "device" in result["message"]
        assert not any(c[0] == "list_saves" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_all_saves_returns_device_not_registered_on_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        # No device_id set.
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        fake.fail_on_next(RommApiError("Server unreachable"))

        result = await svc.sync_all_saves()

        assert result["success"] is False
        assert "Device" in result["message"] or "device" in result["message"]
        # No per-ROM sync ran.
        assert not any(c[0] == "list_saves" for c in fake.call_log)


class TestPostExitServerOfflineGuard:
    """post_exit_sync probes heartbeat first; on failure returns offline=True
    instead of attempting upload (engine.py lines 358-360)."""

    @pytest.mark.asyncio
    async def test_post_exit_sync_returns_offline_when_heartbeat_raises(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")
        fake.heartbeat_raises = RommApiError("Connection refused")

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result["offline"] is True
        assert result["synced"] == 0
        # No upload was attempted after heartbeat failed.
        assert not any(c[0] == "upload_save" for c in fake.call_log)


class TestSyncRomSavesDisabledGuard:
    """Public sync_rom_saves returns failure when save sync is disabled
    (engine.py line 396)."""

    @pytest.mark.asyncio
    async def test_sync_rom_saves_disabled_returns_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        # save_sync_enabled stays False by default.
        result = await svc.sync_rom_saves(42)

        assert result["success"] is False
        assert "disabled" in result["message"].lower()
        assert result["synced"] == 0
        # No list_saves issued — the guard fired before sync ran.
        assert not any(c[0] == "list_saves" for c in fake.call_log)


class TestSyncCallableErrorMessages:
    """The error-count clause in each public callable's success message
    (engine.py lines 318 / 380 / 414). Driven by stubbing _sync_rom_saves
    to return a non-empty errors list."""

    @pytest.mark.asyncio
    async def test_pre_launch_sync_message_includes_error_count(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id):
            return (0, ["pokemon.srm: bad gateway"], [])

        svc._sync_engine._sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert "1 error(s)" in result["message"]
        assert "Downloaded" in result["message"]

    @pytest.mark.asyncio
    async def test_post_exit_sync_message_includes_error_count(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id):
            return (0, ["pokemon.srm: timeout"], [])

        svc._sync_engine._sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert "1 error(s)" in result["message"]
        assert "Uploaded" in result["message"]

    @pytest.mark.asyncio
    async def test_sync_rom_saves_message_includes_error_count(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id):
            return (0, ["pokemon.srm: 502 bad gateway"], [])

        svc._sync_engine._sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.sync_rom_saves(42)

        assert result["success"] is False
        assert "1 error(s)" in result["message"]


class TestSyncEngineDelegates:
    """Cover the thin delegate methods on SyncEngine that forward to MatrixExecutor
    or DeviceRegistry (engine.py lines 204 / 220 / 239)."""

    def test_adopt_baseline_hash_delegates_to_matrix(self, tmp_path):
        """SyncEngine._adopt_baseline_hash writes through to the matrix's state."""
        svc, _ = make_service(tmp_path)

        svc._sync_engine._adopt_baseline_hash("42", "pokemon.srm", "deadbeef" * 4)

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.last_sync_hash == "deadbeef" * 4

    def test_build_sync_conflict_entry_delegates_to_matrix(self, tmp_path):
        """SyncEngine._build_sync_conflict_entry builds the same dict shape as the matrix."""
        svc, _ = make_service(tmp_path)
        server = _server_save(save_id=77, filename="pokemon.srm", file_size_bytes=2048)

        entry = svc._sync_engine._build_sync_conflict_entry(
            rom_id=42,
            filename="pokemon.srm",
            server=server,
            local_path=None,
            local_hash=None,
        )

        assert entry["type"] == "sync_conflict"
        assert entry["rom_id"] == 42
        assert entry["filename"] == "pokemon.srm"
        assert entry["server_save_id"] == 77
        assert entry["server_size"] == 2048
        assert "created_at" in entry

    @pytest.mark.asyncio
    async def test_list_devices_delegates_to_device_registry(self, tmp_path):
        """SyncEngine.list_devices forwards to DeviceRegistry.list_devices."""
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.server_device_id = "device-1"
        # Seed a registered device on the fake so list_devices returns non-empty.
        fake._registered_devices.append({"id": "device-1", "name": "test-host"})

        result = await svc._sync_engine.list_devices()

        assert result["success"] is True
        assert len(result["devices"]) == 1
        assert result["devices"][0]["is_current_device"] is True


class TestGetServerSaveHashNonRetryable:
    """get_server_save_hash swallows non-retryable errors and returns None
    (matrix.py line 130). The retryable-raise path (line 129) is already
    covered by TestResolveSyncConflict.test_resolve_keep_local_falls_back_*."""

    def test_get_server_save_hash_returns_none_on_non_retryable_error(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        # download_save raises, and retry.is_retryable returns False (default
        # in _make_retry), so the matrix should swallow and return None.
        def _raise_on_download(save_id: int, dest_path: str) -> None:
            fake.call_log.append(("download_save", (save_id, dest_path), {}))
            raise RommApiError("permanent failure")

        fake.download_save = _raise_on_download  # type: ignore[method-assign]

        result = svc._sync_engine._matrix.get_server_save_hash({"id": 100})
        assert result is None
        # download_save was attempted exactly once.
        download_calls = [c for c in fake.call_log if c[0] == "download_save"]
        assert len(download_calls) == 1

    def test_get_server_save_hash_returns_none_when_save_id_missing(self, tmp_path):
        """No save_id on the server-save dict → short-circuit to None (line 120)."""
        svc, _ = make_service(tmp_path)

        result = svc._sync_engine._matrix.get_server_save_hash({"file_name": "x.srm"})
        assert result is None


class TestHandleUnexpectedError:
    """_handle_unexpected_error records the error and cleans up the .tmp file
    (matrix.py lines 322-326). Reached from _dispatch_sync_action's generic
    except branch (line 480-481)."""

    def test_dispatch_sync_action_handles_unexpected_exception(self, tmp_path):
        """A non-RommApiError raised during dispatch is classified, recorded,
        and the .tmp file is cleaned up."""
        from domain.sync_action import Download

        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        saves_dir = tmp_path / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)
        # Seed a .tmp file at the expected path — the cleanup branch must remove it.
        tmp_file = saves_dir / "pokemon.srm.tmp"
        tmp_file.write_bytes(b"partial download")
        assert tmp_file.exists()

        # do_download_save is reached for Download action; make it raise an
        # unexpected (non-RommApi) Exception so _handle_unexpected_error fires.
        def _raise(*_args, **_kwargs):
            raise RuntimeError("disk full")

        fake.download_save_content = _raise  # type: ignore[method-assign]

        errors: list[str] = []
        conflicts: list = []
        action = Download(server_save={"id": 100, "file_name": "pokemon.srm"})
        synced = svc._sync_engine._matrix._dispatch_sync_action(
            action,
            rom_id=42,
            rom_id_str="42",
            filename="pokemon.srm",
            local_path=None,
            local_hash=None,
            saves_dir=str(saves_dir),
            system="gba",
            server_saves=[],
            errors=errors,
            conflicts=conflicts,
        )

        assert synced is False
        assert len(errors) == 1
        assert errors[0].startswith("pokemon.srm:")
        # The .tmp file was removed by the cleanup branch.
        assert not tmp_file.exists()


class TestDispatchSyncActionErrorBranches:
    """_dispatch_sync_action's typed-error branches (matrix.py lines 476-481).
    RommApiError → classify + record; other Exception → _handle_unexpected_error."""

    def test_dispatch_sync_action_records_rommapi_error(self, tmp_path):
        """A RommApiError from a Download action is recorded with classify_error
        message; no .tmp cleanup is attempted on this branch."""
        from domain.sync_action import Download

        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        saves_dir = tmp_path / "saves" / "gba"
        saves_dir.mkdir(parents=True, exist_ok=True)

        def _raise(*_args, **_kwargs):
            raise RommApiError("upstream 502")

        fake.download_save_content = _raise  # type: ignore[method-assign]

        errors: list[str] = []
        conflicts: list = []
        action = Download(server_save={"id": 100, "file_name": "pokemon.srm"})
        synced = svc._sync_engine._matrix._dispatch_sync_action(
            action,
            rom_id=42,
            rom_id_str="42",
            filename="pokemon.srm",
            local_path=None,
            local_hash=None,
            saves_dir=str(saves_dir),
            system="gba",
            server_saves=[],
            errors=errors,
            conflicts=conflicts,
        )

        assert synced is False
        assert len(errors) == 1
        assert errors[0].startswith("pokemon.srm:")


class TestDispatchUploadDefensiveBranches:
    """_dispatch_upload's defensive guards (matrix.py lines 408-409, 419-422).
    Both paths are unreachable from the algorithm's normal output but the
    branches exist to keep a future caller's bug from corrupting state."""

    def test_dispatch_upload_records_error_when_local_path_missing(self, tmp_path):
        """Upload(target_save_id=None) with local_path=None records an error and skips."""
        from domain.sync_action import Upload

        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        errors: list[str] = []
        result = svc._sync_engine._matrix._dispatch_upload(
            Upload(target_save_id=None),
            rom_id=42,
            rom_id_str="42",
            filename="pokemon.srm",
            local_path=None,
            system="gba",
            server_saves=[],
            errors=errors,
        )

        assert result is False
        assert len(errors) == 1
        assert "upload requested but no local file" in errors[0]
        # No upload was attempted.
        assert not any(c[0] == "upload_save" for c in fake.call_log)

    def test_dispatch_upload_skips_put_when_target_save_id_vanished(self, tmp_path):
        """Upload(target_save_id=999) with server_saves missing that id is a
        best-effort skip — no upload, no error (vanished between read and dispatch)."""
        from domain.sync_action import Upload

        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"local-edited")

        errors: list[str] = []
        # server_saves does not contain id=999.
        result = svc._sync_engine._matrix._dispatch_upload(
            Upload(target_save_id=999),
            rom_id=42,
            rom_id_str="42",
            filename="pokemon.srm",
            local_path=str(save_path),
            system="gba",
            server_saves=[{"id": 100, "file_name": "pokemon.srm"}],
            errors=errors,
        )

        assert result is False
        # No error recorded — this is a best-effort skip, not a failure.
        assert errors == []
        # No upload was attempted.
        assert not any(c[0] == "upload_save" for c in fake.call_log)


class TestPreLaunchSaveSortGate:
    """pre_launch_sync short-circuits when a save-sort migration is pending
    (engine.py line 297-303)."""

    @pytest.mark.asyncio
    async def test_pre_launch_sync_returns_save_sort_changed(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.settings.save_sync_enabled = True
        svc._save_sync_state.device_id = "test-device"
        _install_rom(svc, tmp_path)
        # Flag save-sort changed via the rom_info state path used by RomInfoService.
        svc._state["save_sort_settings"] = {"sort_by_content": True, "sort_by_core": False}
        svc._state["save_sort_settings_previous"] = {"sort_by_content": False, "sort_by_core": False}

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert result["save_sort_changed"] is True
        assert result["synced"] == 0
        # No sync ran.
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)


class TestRecordOwnUploadNoneId:
    """_record_own_upload guards against new_id=None (matrix.py line 305-306)."""

    def test_record_own_upload_no_op_when_new_id_is_none(self, tmp_path):
        """Passing new_id=None must not touch own_upload_ids."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        # Pre-seed own_upload_ids to assert it stays unchanged.
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {},
                "system": "gba",
                "active_slot": "default",
                "own_upload_ids": [50, 51],
            }
        )

        svc._sync_engine._matrix._record_own_upload("42", None)

        assert svc._save_sync_state.saves["42"].own_upload_ids == [50, 51]
