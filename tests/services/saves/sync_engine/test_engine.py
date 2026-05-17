"""Tests for SyncEngine — public-callable orchestration: lock dispatch, save-sync
gates (enabled, migration-pending, save-sort changed), heartbeat probe,
device-registration fallback, error/conflict count surfacing, and matrix/registry
delegate wiring. Per-file matrix dispatch lives in tests/services/saves/sync_engine/test_matrix.py;
device registration in tests/services/saves/sync_engine/test_devices.py;
conflict rollback in tests/services/saves/sync_engine/test_rollback.py.
"""

import logging

import pytest

from lib.errors import RommApiError
from tests.services.saves._helpers import (
    _create_save,
    _install_rom,
    _server_save,
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
