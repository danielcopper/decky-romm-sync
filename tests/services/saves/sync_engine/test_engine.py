"""Tests for SyncEngine — public-callable orchestration: lock dispatch, save-sync
gates (enabled, migration-pending, save-sort changed), heartbeat probe,
device-registration fallback, error/conflict count surfacing, and matrix/registry
delegate wiring. Per-file matrix dispatch lives in tests/services/saves/sync_engine/test_matrix.py;
device registration in tests/services/saves/sync_engine/test_devices.py;
conflict rollback in tests/services/saves/sync_engine/test_rollback.py.
"""

import asyncio
import io
import logging
import struct
import threading
import time
import zipfile

import pytest

from domain.rom_save_sync_state import RomSaveSyncState
from domain.save_layout import ContentDir, InSaveDir
from lib.errors import (
    RommApiError,
    RommAuthError,
    RommConnectionError,
    RommForbiddenError,
    RommSSLError,
    RommSyncDisabledError,
    RommTimeoutError,
)
from lib.list_result import ErrorCode
from services.saves._messages import DEVICE_SYNC_DISABLED, DEVICE_SYNC_DISABLED_REASON
from services.saves.sync_engine.engine import _first_error_reason, _summarize_sync_result
from tests.services.saves._helpers import (
    _create_save,
    _do_sync,
    _enable_sync_with_device,
    _get_device_id,
    _install_rom,
    _require_save_state,
    _seed_save_state,
    _server_save,
    _server_save_with_syncs,
    _set_device_id,
    _set_sort_settings,
    _set_sort_settings_previous,
    make_service,
)


def _corrupt_zip_bytes() -> bytes:
    """Bytes that ``zipfile.is_zipfile`` accepts but ``ZipFile`` cannot open.

    A real two-member zip with its central-directory signature clobbered — the
    #1470 poison: an intact End-Of-Central-Directory record makes it sniff as a
    zip, but reading it raises ``BadZipFile``, the failure that used to escape
    the sweep and abort every remaining ROM.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("battery.srm", b"battery-bytes")
        zf.writestr("rtc.bin", b"rtc-bytes")
    data = bytearray(buf.getvalue())
    cd_offset = struct.unpack("<I", data[-22:][16:20])[0]  # EOCD → central-dir offset
    data[cd_offset : cd_offset + 4] = b"\x00\x00\x00\x00"  # kill the PK\x01\x02 magic
    return bytes(data)


class TestSyncRomSaves:
    def test_local_only_uploads(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "dev-1")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"save data")

        uploaded, downloaded, errors, conflicts = _do_sync(svc, 42)
        assert uploaded == 1
        assert downloaded == 0
        assert errors == []
        assert conflicts == []
        assert any(c[0] == "upload_save" for c in fake.call_log)

    def test_server_only_downloads(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        # Add server save but no local file
        ss = _server_save()
        fake.saves[100] = ss

        uploaded, downloaded, errors, _ = _do_sync(svc, 42)
        assert uploaded == 0
        assert downloaded == 1
        assert errors == []
        # Verify the file was downloaded
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()

    def test_rom_not_installed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        uploaded, downloaded, errors, _ = _do_sync(svc, 999)
        assert uploaded == 0
        assert downloaded == 0
        assert errors == []

    def test_api_error_on_list_saves(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        fake.fail_on_next(RommApiError("Server error"))

        uploaded, downloaded, errors, _ = _do_sync(svc, 42)
        assert uploaded == 0
        assert downloaded == 0
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
        svc._config.settings["save_sync_enabled"] = True
        _install_rom(svc, tmp_path)
        # Mark migration pending — detect has fired, user hasn't resolved yet.
        _set_sort_settings(svc, {"sort_by_content": True, "sort_by_core": False})
        _set_sort_settings_previous(svc, {"sort_by_content": True, "sort_by_core": False})
        # Server has a save, no local file anywhere.
        ss = _server_save()
        fake.saves[100] = ss

        uploaded, downloaded, errors, conflicts = _do_sync(svc, 42)

        assert uploaded == 0
        assert downloaded == 0
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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "dev-1")
        _install_rom(svc, tmp_path)
        _set_sort_settings(svc, {"sort_by_content": True, "sort_by_core": False})
        _set_sort_settings_previous(svc, {"sort_by_content": True, "sort_by_core": False})
        # Local save at the (previous == current, same layout) location.
        _create_save(tmp_path, content=b"user progress")

        uploaded, downloaded, errors, conflicts = _do_sync(svc, 42)

        assert uploaded == 1
        assert downloaded == 0
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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        orig_sync = svc._sync_engine.do_sync_rom_saves

        def wrapped_sync(rom_id, *args):
            call_order.append("sync")
            return orig_sync(rom_id, *args)

        svc._sync_engine.do_sync_rom_saves = wrapped_sync  # type: ignore[method-assign]

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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        # Stub do_sync_rom_saves to return 1 conflict, 0 synced, 0 errors
        def stub_sync(rom_id, *args):
            return (0, 0, [], [{"type": "newer_in_slot", "rom_id": rom_id}])

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.sync_rom_saves(42)

        # success is still True — conflicts are legitimate state, not technical failure
        assert result["success"] is True
        assert "1 conflict(s)" in result["message"]
        assert result["synced"] == 0


class TestSyncAllSaves:
    @pytest.mark.asyncio
    async def test_syncs_multiple_roms(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")

        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _seed_save_state(
            svc, 2, RomSaveSyncState(system="snes", slot_confirmed=True, active_slot="default"), platform_slug="snes"
        )
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"save2")
        # Both confirmed non-legacy ROMs decide via the local matrix (ADR-0017),
        # POSTing their local-only saves. One whole-device transport session wraps
        # the sweep.

        result = await svc.sync_all_saves()
        assert result["success"] is True
        assert result["synced"] == 2
        assert result["roms_checked"] == 2
        # Exactly one whole-device transport session for both ROMs.
        assert len([c for c in fake.call_log if c[0] == "negotiate_sync"]) == 1

    @pytest.mark.asyncio
    async def test_unreadable_zip_save_does_not_abort_sweep(self, tmp_path):
        """#1470 — a save that sniffs as a zip but cannot be read as one must not
        crash the whole sweep: the other ROM still syncs, and the poison ROM falls
        back to its plain MD5 and uploads like any new save. Before the adapter
        fallback, the matrix per-file hash raised ``BadZipFile`` and aborted the run.
        """
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")

        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _seed_save_state(
            svc, 2, RomSaveSyncState(system="snes", slot_confirmed=True, active_slot="default"), platform_slug="snes"
        )
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"good-save")
        # ROM 2's save is the poison: it sniffs as a zip but cannot be read as one.
        _create_save(tmp_path, system="snes", rom_name="game2", content=_corrupt_zip_bytes())

        result = await svc.sync_all_saves()

        # The run completed (no BadZipFile escaped) and swept both ROMs cleanly.
        assert result["success"] is True
        assert result["roms_checked"] == 2
        assert result["errors"] == []
        assert result["synced"] == 2
        # Both uploaded — the good save normally, the poison via its MD5 fallback.
        uploaded_roms = {c[1][0] for c in fake.call_log if c[0] == "upload_save"}
        assert uploaded_roms == {1, 2}

    @pytest.mark.asyncio
    async def test_disabled_returns_early(self, tmp_path):
        svc, _ = make_service(tmp_path)
        result = await svc.sync_all_saves()
        assert result["success"] is False
        assert "disabled" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_partial_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")

        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _seed_save_state(
            svc, 2, RomSaveSyncState(system="snes", slot_confirmed=True, active_slot="default"), platform_slug="snes"
        )
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"save2")
        # Both confirmed non-legacy ROMs POST via the matrix; make the second
        # ROM's upload fail.

        original_upload = fake.upload_save

        def flaky_upload(rom_id, *args, **kwargs):
            if rom_id == 2:
                raise RommApiError("Server error")
            return original_upload(rom_id, *args, **kwargs)

        fake.upload_save = flaky_upload

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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")

        # Every ROM decides via the local matrix now (ADR-0017) — the worker is
        # do_sync_rom_saves, wrapped to record call ordering.
        orig_sync = svc._sync_engine.do_sync_rom_saves

        def wrapped_sync(rom_id, *args):
            call_order.append("sync")
            return orig_sync(rom_id, *args)

        svc._sync_engine.do_sync_rom_saves = wrapped_sync  # type: ignore[method-assign]

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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))

        # Stub the matrix worker to produce conflicts but no errors (every ROM
        # decides via do_sync_rom_saves now, ADR-0017).
        def stub_sync(rom_id, *args):
            return (0, 0, [], [{"type": "newer_in_slot", "rom_id": rom_id}])

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.sync_all_saves()

        assert result["success"] is True
        assert result["conflicts"] >= 1
        assert "conflict(s)" in result["message"]

    # ------------------------------------------------------------------
    # #1055: the bulk sweep gates each ROM on slot confirmation so a
    # never-configured ROM's stale local save can't be auto-uploaded into
    # the default slot and overwrite another device's newer progress.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_confirmed_rom_is_swept_and_uploads(self, tmp_path):
        """A ROM whose slot the user has confirmed IS swept and uploads (#1055)."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")
        # Confirmed non-legacy → the matrix POSTs the local-only save.

        result = await svc.sync_all_saves()

        assert result["success"] is True
        assert result["synced"] == 1
        assert result["roms_checked"] == 1
        assert any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_unconfirmed_rom_is_skipped_no_upload(self, tmp_path):
        """A never-configured ROM is SKIPPED — no upload, no download (#1055).

        Non-vacuous: the assertion that NO ``upload_save`` reached the server is
        what proves the slot-confirmation gate fired before any transfer.
        """
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        # No save state seeded → slot_confirmed defaults to False → never configured.
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")

        result = await svc.sync_all_saves()

        assert result["synced"] == 0
        assert result["roms_checked"] == 1
        assert not any(c[0] == "upload_save" for c in fake.call_log)
        assert not any(c[0] == "download_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_mixed_only_confirmed_rom_syncs(self, tmp_path):
        """With one confirmed and one unconfirmed ROM, only the confirmed one syncs (#1055)."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        # rom 2 left unconfirmed (no save state seeded).
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"save2")
        # Only the confirmed ROM (rom 1) is swept; rom 2 is gated out unconfirmed,
        # so the single matrix upload is for rom 1 alone.

        result = await svc.sync_all_saves()

        assert result["synced"] == 1
        assert result["roms_checked"] == 2
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # The single upload was for the confirmed ROM (rom_id is the first positional arg).
        assert upload_calls[0][1][0] == 1


class TestPreLaunchSync:
    @pytest.mark.asyncio
    async def test_downloads_server_saves(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
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
        svc._config.settings["save_sync_enabled"] = True
        svc._config.settings["sync_before_launch"] = False
        _set_device_id(svc, "test-device")

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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")

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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
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
        svc._config.settings["save_sync_enabled"] = True
        svc._config.settings["sync_after_exit"] = False
        _set_device_id(svc, "test-device")

        result = await svc.post_exit_sync(42)
        assert result["synced"] == 0

    @pytest.mark.asyncio
    async def test_auto_registers_device(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        # No device_id set
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")

        result = await svc.post_exit_sync(42)
        assert result["success"] is True
        assert _get_device_id(svc) is not None

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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        # Patch do_sync_rom_saves to record call ordering.
        orig_sync = svc._sync_engine.do_sync_rom_saves

        def wrapped_sync(rom_id, *args):
            call_order.append("sync")
            return orig_sync(rom_id, *args)

        svc._sync_engine.do_sync_rom_saves = wrapped_sync  # type: ignore[method-assign]

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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
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
    async def test_post_exit_sync_message_includes_conflict_count(self, tmp_path):
        """post_exit_sync must surface conflict count in its message.

        Previously "Uploaded 0 save(s)" even with conflicts — user has no
        signal that sync is blocked on manual resolution.
        """
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id, *args):
            return (0, 0, [], [{"type": "newer_in_slot", "rom_id": rom_id}])

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is True
        assert "1 conflict(s)" in result["message"]
        assert result["synced"] == 0


class TestDeviceSyncDisabled:
    """RomM's per-device sync-disabled 400 (#1489) stops the run with a policy reason.

    The negotiate ``RommSyncDisabledError`` is re-raised out of the openers and
    caught at each entry point; every OTHER negotiate failure keeps the existing
    degrade-to-sessionless behavior, and pre-launch skips silently like the local
    toggle so the launch always proceeds.
    """

    @staticmethod
    def _seed_confirmed_rom(
        svc,
        tmp_path,
        *,
        rom_id=42,
        system="gba",
        rom_name="pokemon",
        file_name="pokemon.gba",
        content=b"save data",
    ):
        """Install a ROM with a confirmed named slot and a local-only save file."""
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path, rom_id=rom_id, system=system, file_name=file_name)
        _seed_save_state(
            svc,
            rom_id,
            RomSaveSyncState(system=system, slot_confirmed=True, active_slot="default"),
            platform_slug=system,
        )
        _create_save(tmp_path, system=system, rom_name=rom_name, content=content)

    @pytest.mark.asyncio
    async def test_open_negotiate_session_degrades_to_none_on_generic_error(self, tmp_path):
        """A transient negotiate failure still degrades to a sessionless run (None)."""
        svc, fake = make_service(tmp_path)
        self._seed_confirmed_rom(svc, tmp_path)
        fake.fail_on_next(RommConnectionError("transient"))
        session_id = await svc._sync_engine._open_negotiate_session(42, "test-device")
        assert session_id is None

    @pytest.mark.asyncio
    async def test_open_negotiate_session_reraises_policy_error(self, tmp_path):
        """The per-device sync-disabled 400 is re-raised, not swallowed like transports."""
        svc, fake = make_service(tmp_path)
        self._seed_confirmed_rom(svc, tmp_path)
        fake.negotiate_sync_disabled = True
        with pytest.raises(RommSyncDisabledError):
            await svc._sync_engine._open_negotiate_session(42, "test-device")

    @pytest.mark.asyncio
    async def test_sync_rom_saves_returns_policy_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        self._seed_confirmed_rom(svc, tmp_path)
        fake.negotiate_sync_disabled = True

        result = await svc.sync_rom_saves(42)

        assert result["success"] is False
        assert result["reason"] == DEVICE_SYNC_DISABLED_REASON
        assert result["message"] == DEVICE_SYNC_DISABLED
        assert result["synced"] == 0
        assert result["errors"] == []
        assert result["conflicts"] == []
        # Aborted before the matrix — no upload happened.
        assert not any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_post_exit_sync_returns_policy_failure(self, tmp_path):
        svc, fake = make_service(tmp_path)
        self._seed_confirmed_rom(svc, tmp_path)
        fake.negotiate_sync_disabled = True

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result["reason"] == DEVICE_SYNC_DISABLED_REASON
        assert result["message"] == DEVICE_SYNC_DISABLED
        assert result["synced"] == 0
        assert not any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_pre_launch_sync_silently_skips(self, tmp_path):
        """Pre-launch mirrors the local toggle-off skip: success shape, no offline/reason routing."""
        svc, fake = make_service(tmp_path)
        self._seed_confirmed_rom(svc, tmp_path)
        fake.negotiate_sync_disabled = True

        result = await svc.pre_launch_sync(42)

        assert result["success"] is True
        assert result["message"] == DEVICE_SYNC_DISABLED
        assert result["synced"] == 0
        # Must NOT route into the offline / launch-gate / failure flow.
        assert "offline" not in result
        assert "reason" not in result
        assert not any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_all_saves_bulk_abort_returns_policy_failure(self, tmp_path):
        """The whole-device bulk pre-negotiate hits the switch → abort before the sweep."""
        svc, fake = make_service(tmp_path)
        self._seed_confirmed_rom(svc, tmp_path, rom_id=1, rom_name="game1", file_name="game1.gba", content=b"s1")
        self._seed_confirmed_rom(
            svc, tmp_path, rom_id=2, system="snes", rom_name="game2", file_name="game2.sfc", content=b"s2"
        )
        fake.negotiate_sync_disabled = True

        result = await svc.sync_all_saves()

        assert result["success"] is False
        assert result["reason"] == DEVICE_SYNC_DISABLED_REASON
        assert result["message"] == DEVICE_SYNC_DISABLED
        assert result["synced"] == 0
        assert result["roms_checked"] == 0
        assert not any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_all_saves_midsweep_abort_reports_partial_totals(self, tmp_path):
        """When the bulk session degrades and a LATER per-ROM negotiate hits the switch,
        the sweep aborts reporting the partial totals synced so far (#1489)."""
        svc, fake = make_service(tmp_path)
        self._seed_confirmed_rom(svc, tmp_path, rom_id=1, rom_name="game1", file_name="game1.gba", content=b"s1")
        self._seed_confirmed_rom(
            svc, tmp_path, rom_id=2, system="snes", rom_name="game2", file_name="game2.sfc", content=b"s2"
        )

        empty = {"operations": [], "total_upload": 0, "total_download": 0, "total_conflict": 0, "total_no_op": 0}
        calls = {"n": 0}

        def negotiate(device_id, saves):
            calls["n"] += 1
            if calls["n"] == 1:
                # Bulk pre-negotiate degrades (no session_id) → per-ROM sessions.
                return {"session_id": None, **empty}
            if calls["n"] == 2:
                # ROM 1's per-ROM session opens fine → it syncs and uploads.
                return {"session_id": 100, **empty}
            # ROM 2's per-ROM negotiate hits the per-device switch mid-sweep.
            raise RommSyncDisabledError("Sync is disabled for this device", url="/api/sync/negotiate", method="POST")

        fake.negotiate_sync = negotiate  # type: ignore[method-assign]

        result = await svc.sync_all_saves()

        assert result["success"] is False
        assert result["reason"] == DEVICE_SYNC_DISABLED_REASON
        assert result["synced"] == 1
        assert "after syncing 1 save(s)" in result["message"]
        assert result["roms_checked"] == 2
        # ROM 1 uploaded before the abort; ROM 2 never did.
        uploaded = {c[1][0] for c in fake.call_log if c[0] == "upload_save"}
        assert uploaded == {1}


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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"unsyncable")

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result["blocked_by_migration"] is True
        assert result["synced"] == 0
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)


class TestPostExitServerOfflineGuard:
    """post_exit_sync probes heartbeat first. A genuine reachability failure
    (connection/timeout) returns offline=True; an auth/SSL failure flows through
    classify_error so it carries its OWN reason + message instead of masking the
    reachable server as offline (#971)."""

    @pytest.mark.asyncio
    async def test_post_exit_sync_returns_offline_when_connection_error(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")
        fake.heartbeat_raises = RommConnectionError("Connection refused")

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result["offline"] is True
        assert result["reason"] == ErrorCode.SERVER_UNREACHABLE.value
        assert result["message"] == "Server offline"
        assert result["synced"] == 0
        # No upload was attempted after heartbeat failed.
        assert not any(c[0] == "upload_save" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_post_exit_sync_returns_offline_when_timeout(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")
        fake.heartbeat_raises = RommTimeoutError("timed out")

        result = await svc.post_exit_sync(42)

        assert result["offline"] is True
        assert result["reason"] == ErrorCode.SERVER_UNREACHABLE.value
        assert result["message"] == "Server offline"

    @pytest.mark.asyncio
    async def test_post_exit_sync_auth_failure_is_not_offline(self, tmp_path):
        """A revoked token (401) on the heartbeat must NOT read as offline."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")
        fake.heartbeat_raises = RommAuthError("401 Unauthorized")

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert "offline" not in result
        assert result["reason"] == ErrorCode.AUTH_FAILED.value
        assert "uthentication failed" in result["message"]
        assert result["message"] != "Server offline"
        assert not any(c[0] == "upload_save" for c in fake.call_log)


class TestPreLaunchServerOfflineGuard:
    """pre_launch_sync probes heartbeat first (F4). A reachability failure
    returns the canonical SERVER_UNREACHABLE shape + offline=True; an auth/SSL
    failure flows through classify_error to its OWN reason + DISTINCT message so
    the UI stops claiming the server is unreachable (#971)."""

    @pytest.mark.asyncio
    async def test_pre_launch_sync_returns_offline_when_connection_error(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        fake.saves[100] = _server_save()
        fake.heartbeat_raises = RommConnectionError("Connection refused")

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert result["offline"] is True
        assert result["reason"] == ErrorCode.SERVER_UNREACHABLE.value
        assert result["message"] == "Server offline"
        assert result["synced"] == 0
        # No download/list was attempted after heartbeat failed.
        assert not any(c[0] in ("download_save", "list_saves") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_pre_launch_sync_returns_offline_when_timeout(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        fake.saves[100] = _server_save()
        fake.heartbeat_raises = RommTimeoutError("timed out")

        result = await svc.pre_launch_sync(42)

        assert result["offline"] is True
        assert result["reason"] == ErrorCode.SERVER_UNREACHABLE.value
        assert result["message"] == "Server offline"

    @pytest.mark.asyncio
    async def test_pre_launch_sync_auth_failure_is_classified_not_offline(self, tmp_path):
        """A 401 on the heartbeat surfaces AUTH_FAILED with a distinct message,
        never the misleading "Server offline" + offline flag (#971)."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        fake.saves[100] = _server_save()
        fake.heartbeat_raises = RommAuthError("401 Unauthorized")

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert "offline" not in result
        assert result["reason"] == ErrorCode.AUTH_FAILED.value
        assert "uthentication failed" in result["message"]
        assert result["message"] != "Server offline"
        assert result["synced"] == 0
        assert not any(c[0] in ("download_save", "list_saves") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_pre_launch_sync_ssl_failure_keeps_unreachable_slug_distinct_message(self, tmp_path):
        """An SSL misconfig classifies to SERVER_UNREACHABLE but with the SSL
        message — NOT the literal "Server offline" and NO offline flag."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        fake.saves[100] = _server_save()
        fake.heartbeat_raises = RommSSLError("cert verify failed")

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert "offline" not in result
        assert result["reason"] == ErrorCode.SERVER_UNREACHABLE.value
        assert "SSL" in result["message"]
        assert result["message"] != "Server offline"

    @pytest.mark.asyncio
    async def test_pre_launch_sync_offline_branch_logs_at_debug(self, tmp_path):
        """The offline branch is no longer a silent swallow — the raw exception
        is logged via the injected DebugLogger (asserted non-vacuously)."""
        debug_log: list[str] = []
        svc, fake = make_service(tmp_path, log_debug=debug_log.append)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        fake.saves[100] = _server_save()
        fake.heartbeat_raises = RommConnectionError("Connection refused")

        await svc.pre_launch_sync(42)

        assert any("heartbeat failed" in m and "Connection refused" in m for m in debug_log)

    @pytest.mark.asyncio
    async def test_pre_launch_sync_proceeds_when_heartbeat_ok(self, tmp_path):
        """Control: a healthy heartbeat (default) lets the download proceed."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        fake.saves[100] = _server_save()

        result = await svc.pre_launch_sync(42)

        assert result["success"] is True
        assert result["synced"] == 1
        assert "offline" not in result
        assert any(c[0] == "heartbeat" for c in fake.call_log)


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
    """The failure ``message`` each public sync callable builds from the matrix
    result (``_summarize_sync_result``). A total failure leads with the first
    error's classified reason (never buried behind "Uploaded 0 save(s)"); a
    partial run keeps the count summary and appends the reason; pre-launch
    (download) keeps the plain count clause (#1334). Driven by stubbing
    do_sync_rom_saves to return a non-empty errors list."""

    @pytest.mark.asyncio
    async def test_pre_launch_sync_message_includes_error_count(self, tmp_path):
        # pre_launch keeps the plain count clause — the reason promotion is
        # scoped to the upload-side callables (#1334).
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id, *args):
            return (0, 0, ["pokemon.srm: bad gateway"], [])

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert "1 error(s)" in result["message"]
        assert "Downloaded" in result["message"]

    @pytest.mark.asyncio
    async def test_post_exit_sync_promotes_reason_when_nothing_uploaded(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id, *args):
            return (0, 0, ["pokemon.srm: timeout"], [])

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        # A total failure leads with the bare reason, not "Uploaded 0 save(s)".
        assert result["message"] == "timeout"

    @pytest.mark.asyncio
    async def test_post_exit_sync_partial_keeps_count_and_appends_reason(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id, *args):
            return (2, 0, ["pokemon.srm: timeout"], [])

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result["message"] == "Uploaded 2 save(s), 1 error(s) — timeout"

    @pytest.mark.asyncio
    async def test_post_exit_sync_multiple_errors_count_the_extras(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id, *args):
            return (0, 0, ["a.srm: timeout", "b.srm: timeout", "c.srm: timeout"], [])

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["message"] == "timeout (+2 more)"

    @pytest.mark.asyncio
    async def test_sync_rom_saves_promotes_reason_when_nothing_synced(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id, *args):
            return (0, 0, ["pokemon.srm: 502 bad gateway"], [])

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.sync_rom_saves(42)

        assert result["success"] is False
        assert result["message"] == "502 bad gateway"


class TestSyncCallablesSurfaceDirectionCounts:
    """The per-ROM sync callables surface per-direction counts (#250).

    The completion toast names which way saves moved, so each result dict
    carries ``uploaded`` / ``downloaded`` alongside the ``synced`` total.
    """

    @pytest.mark.asyncio
    async def test_pre_launch_sync_reports_download_count(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id, *args):
            return (0, 2, [], [])  # two downloads, no upload

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.pre_launch_sync(42)

        assert result["success"] is True
        assert result["uploaded"] == 0
        assert result["downloaded"] == 2
        assert result["synced"] == 2

    @pytest.mark.asyncio
    async def test_post_exit_sync_reports_upload_count(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id, *args):
            return (3, 0, [], [])  # three uploads, no download

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is True
        assert result["uploaded"] == 3
        assert result["downloaded"] == 0
        assert result["synced"] == 3

    @pytest.mark.asyncio
    async def test_sync_rom_saves_reports_mixed_counts(self, tmp_path):
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)

        def stub_sync(rom_id, *args):
            return (1, 2, [], [])  # one up, two down

        svc._sync_engine.do_sync_rom_saves = stub_sync  # type: ignore[method-assign]

        result = await svc.sync_rom_saves(42)

        assert result["success"] is True
        assert result["uploaded"] == 1
        assert result["downloaded"] == 2
        assert result["synced"] == 3


class TestSyncCallablePromotesRealDispatchReason:
    """The promoted message comes from a REAL classified failure dispatched
    through the matrix executor — not a fabricated ``message`` — so a 403 on the
    upload surfaces "Access denied …" in both the post-exit toast and the manual
    sync result (#1334)."""

    @pytest.mark.asyncio
    async def test_post_exit_sync_surfaces_dispatched_403(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        svc._config.settings["sync_after_exit"] = True
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"user progress")

        def forbidden_upload(*_a, **_k):
            raise RommForbiddenError("403")

        fake.upload_save = forbidden_upload  # type: ignore[method-assign]

        result = await svc.post_exit_sync(42)

        assert result["success"] is False
        assert result["synced"] == 0
        assert result["message"] == "Access denied — your account lacks permissions for this action"
        # The raw per-file error still carries the filename for the log surface.
        assert result["errors"][0].startswith("pokemon.srm:")

    @pytest.mark.asyncio
    async def test_sync_rom_saves_surfaces_dispatched_403(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"user progress")

        def forbidden_upload(*_a, **_k):
            raise RommForbiddenError("403")

        fake.upload_save = forbidden_upload  # type: ignore[method-assign]

        result = await svc.sync_rom_saves(42)

        assert result["success"] is False
        assert result["message"] == "Access denied — your account lacks permissions for this action"


class TestSummarizeSyncResult:
    """Value-exact copy for the pure message composer + reason extractor (engine.py)."""

    def test_clean_run_returns_base_unchanged(self):
        assert _summarize_sync_result("Uploaded 3 save(s)", synced=3, errors=[], conflicts=0) == "Uploaded 3 save(s)"

    def test_total_failure_leads_with_bare_reason(self):
        msg = _summarize_sync_result("Uploaded 0 save(s)", synced=0, errors=["a.srm: Access denied"], conflicts=0)
        assert msg == "Access denied"

    def test_total_failure_counts_the_extra_failures(self):
        msg = _summarize_sync_result("Uploaded 0 save(s)", synced=0, errors=["a: x", "b: y", "c: z"], conflicts=0)
        assert msg == "x (+2 more)"

    def test_partial_run_keeps_count_then_appends_reason(self):
        msg = _summarize_sync_result("Uploaded 2 save(s)", synced=2, errors=["a.srm: timeout"], conflicts=0)
        assert msg == "Uploaded 2 save(s), 1 error(s) — timeout"

    def test_conflicts_suffix_on_clean_run(self):
        msg = _summarize_sync_result("Synced 1 save(s)", synced=1, errors=[], conflicts=2)
        assert msg == "Synced 1 save(s), 2 conflict(s)"

    def test_conflicts_suffix_after_promoted_reason(self):
        msg = _summarize_sync_result("Uploaded 0 save(s)", synced=0, errors=["a: boom"], conflicts=1)
        assert msg == "boom, 1 conflict(s)"

    def test_first_error_reason_strips_the_filename_prefix(self):
        assert _first_error_reason(["pokemon.srm: Access denied — nope"]) == "Access denied — nope"

    def test_first_error_reason_falls_back_without_a_separator(self):
        assert _first_error_reason(["bare message"]) == "bare message"

    def test_first_error_reason_handles_a_colon_in_the_filename(self):
        # A ROM name may contain a colon ("Grand Theft Auto: San Andreas"), so its
        # save filename does too; splitting on the LAST ": " keeps that colon on
        # the source side and the reason must not leak a filename fragment.
        errors = ["Grand Theft Auto: San Andreas.srm: Access denied — your account lacks permissions for this action"]
        assert _first_error_reason(errors) == "Access denied — your account lacks permissions for this action"

    def test_first_error_reason_truncates_a_reason_that_contains_a_colon(self):
        # Documented tradeoff of the last-separator split: a reason carrying an
        # internal ": " (only the rare str(exc) fallback) loses its head.
        assert _first_error_reason(["game.srm: weird: colon reason"]) == "colon reason"

    def test_summarize_uses_the_bare_reason_for_a_colon_filename(self):
        # End-to-end copy: a total failure on a colon-named ROM's save shows the
        # bare classified reason, never a filename fragment (#1334).
        errors = ["Game: Subtitle.srm: Access denied — your account lacks permissions for this action"]
        msg = _summarize_sync_result("Uploaded 0 save(s)", synced=0, errors=errors, conflicts=0)
        assert msg == "Access denied — your account lacks permissions for this action"


class TestSyncEngineDelegates:
    """Cover the thin delegate methods on SyncEngine that forward to MatrixExecutor
    or DeviceRegistry (engine.py lines 204 / 220 / 239)."""

    def test_adopt_baseline_hash_delegates_to_matrix(self, tmp_path):
        """SyncEngine.adopt_baseline_hash records the hash on the passed aggregate."""
        from domain.rom_save_sync_state import RomSaveSyncState

        svc, _ = make_service(tmp_path)

        state = RomSaveSyncState()
        svc._sync_engine.adopt_baseline_hash(state, "pokemon.srm", "deadbeef" * 4)

        assert state.files["pokemon.srm"].last_sync_hash == "deadbeef" * 4

    def test_build_sync_conflict_entry_delegates_to_matrix(self, tmp_path):
        """SyncEngine.build_sync_conflict_entry builds the same dict shape as the matrix."""
        svc, _ = make_service(tmp_path)
        server = _server_save(save_id=77, filename="pokemon.srm", file_size_bytes=2048)

        entry = svc._sync_engine.build_sync_conflict_entry(
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
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "device-1")
        # Seed a registered device on the fake so list_devices returns non-empty.
        fake._registered_devices.append({"id": "device-1", "name": "test-host"})

        result = await svc._sync_engine.list_devices()

        assert result["success"] is True
        assert len(result["devices"]) == 1
        assert result["devices"][0]["is_current_device"] is True


class TestSaveSyncContentDirGate:
    """All four public sync entry points hard-gate save sync when RetroArch
    writes saves to the content dir (savefiles_in_content_dir=true). The gate
    reads ``_current_layout``, populated from ``detect_sort_change``'s return at
    the top of each flow. The result is the benign-skip shape the frontend
    treats as "skip, no error, launch proceeds" (#239)."""

    _CONTENT_DIR_SKIP_MESSAGE_FRAGMENT = "content directory"

    def _assert_benign_skip(self, result, *, all_saves=False):
        assert result["success"] is False
        assert result["reason"] == "savefiles_in_content_dir"
        assert self._CONTENT_DIR_SKIP_MESSAGE_FRAGMENT in result["message"]
        assert result["synced"] == 0
        assert result["errors"] == []
        if all_saves:
            assert result["conflicts"] == 0
            assert result["conflicts_list"] == []
            assert result["roms_checked"] == 0
        else:
            assert result["conflicts"] == []

    @pytest.mark.asyncio
    async def test_pre_launch_sync_skips_on_content_dir(self, tmp_path):
        svc, fake = make_service(tmp_path, detect_sort_change=lambda: ContentDir())
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.pre_launch_sync(42)

        self._assert_benign_skip(result)
        # No sync ran — the gate fired before any transfer.
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_post_exit_sync_skips_on_content_dir(self, tmp_path):
        svc, fake = make_service(tmp_path, detect_sort_change=lambda: ContentDir())
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"unsyncable")

        result = await svc.post_exit_sync(42)

        self._assert_benign_skip(result)
        # The gate fires before the heartbeat probe and before any upload.
        assert not any(c[0] in ("upload_save", "heartbeat") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_rom_saves_skips_on_content_dir(self, tmp_path):
        svc, fake = make_service(tmp_path, detect_sort_change=lambda: ContentDir())
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"unsyncable")

        result = await svc.sync_rom_saves(42)

        self._assert_benign_skip(result)
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_all_saves_skips_on_content_dir(self, tmp_path):
        svc, fake = make_service(tmp_path, detect_sort_change=lambda: ContentDir())
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")

        result = await svc.sync_all_saves()

        self._assert_benign_skip(result, all_saves=True)
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_in_save_dir_layout_does_not_block(self, tmp_path):
        """Control: a supported InSaveDir layout syncs normally — no gate."""
        svc, _ = make_service(
            tmp_path,
            detect_sort_change=lambda: InSaveDir(sort_by_content=True, sort_by_core=False),
        )
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        result = await svc.sync_rom_saves(42)

        assert result["success"] is True
        assert "reason" not in result
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_detect_failure_fails_open_does_not_block(self, tmp_path):
        """A detect that raises leaves ``_current_layout`` unset — sync proceeds."""

        def boom():
            raise RuntimeError("cfg unreadable")

        svc, _ = make_service(tmp_path, detect_sort_change=boom)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"progress")

        result = await svc.sync_rom_saves(42)

        # Fail-open: no benign-skip reason, sync ran.
        assert "reason" not in result
        assert result["success"] is True
        assert result["synced"] == 1


class TestPreLaunchSaveSortGate:
    """pre_launch_sync short-circuits when a save-sort migration is pending
    (engine.py line 297-303)."""

    @pytest.mark.asyncio
    async def test_pre_launch_sync_returns_save_sort_changed(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        # Flag save-sort changed via the kv_config markers RomInfoService reads.
        _set_sort_settings(svc, {"sort_by_content": True, "sort_by_core": False})
        _set_sort_settings_previous(svc, {"sort_by_content": False, "sort_by_core": False})

        result = await svc.pre_launch_sync(42)

        assert result["success"] is False
        assert result["save_sort_changed"] is True
        assert result["synced"] == 0
        # No sync ran.
        assert not any(c[0] in ("upload_save", "download_save_content") for c in fake.call_log)


class TestSaveSyncDeviceGate:
    """Device-level single-owner serialization gate (#1234 phase 2a).

    Only one save-sync run is in flight at a time per device. A second
    trigger queues behind the in-flight one; the wait is bounded, so a stuck
    run never traps the launch path — on expiry each trigger returns its
    own offline/busy fallthrough. The gate sits OUTSIDE the per-ROM lock.
    """

    async def _assert_timeout_fallthrough(self, svc, monkeypatch, const_name, trigger, expected):
        """Hold the gate + shrink the timeout, then assert the trigger's fallthrough.

        Holds the engine's device-gate lock so the trigger cannot acquire it,
        and rebinds the per-trigger timeout constant to a tiny value so the
        bounded wait expires fast. The trigger must return *expected* verbatim.
        """
        engine = svc._sync_engine
        monkeypatch.setattr(f"services.saves.sync_engine.engine.{const_name}", 0.01)
        await engine._device_gate._lock.acquire()
        try:
            result = await trigger()
        finally:
            engine._device_gate._lock.release()
        assert result == expected
        # The gate is free again once the external holder released it.
        assert engine._device_gate.is_in_flight() is False

    @pytest.mark.asyncio
    async def test_pre_launch_sync_times_out_to_offline(self, tmp_path, monkeypatch):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        fake.saves[100] = _server_save()

        await self._assert_timeout_fallthrough(
            svc,
            monkeypatch,
            "PRE_LAUNCH_GATE_TIMEOUT",
            lambda: svc.pre_launch_sync(42),
            {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Save-sync busy — treating as offline",
                "synced": 0,
                "offline": True,
            },
        )
        # The gate fired before any sync work — no transfer was attempted.
        assert not any(c[0] in ("download_save", "list_saves", "heartbeat") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_post_exit_sync_times_out_to_offline(self, tmp_path, monkeypatch):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")

        await self._assert_timeout_fallthrough(
            svc,
            monkeypatch,
            "POST_EXIT_GATE_TIMEOUT",
            lambda: svc.post_exit_sync(42),
            {
                "success": False,
                "reason": ErrorCode.SERVER_UNREACHABLE.value,
                "message": "Save-sync busy — skipping post-exit sync",
                "synced": 0,
                "offline": True,
            },
        )
        assert not any(c[0] in ("upload_save", "heartbeat") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_rom_saves_times_out_to_busy(self, tmp_path, monkeypatch):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"data")

        await self._assert_timeout_fallthrough(
            svc,
            monkeypatch,
            "SYNC_ROM_GATE_TIMEOUT",
            lambda: svc.sync_rom_saves(42),
            {
                "success": False,
                "reason": "sync_busy",
                "message": "Another save-sync run is in progress",
                "synced": 0,
                "errors": [],
                "conflicts": [],
            },
        )
        assert not any(c[0] in ("upload_save", "list_saves") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_sync_all_saves_times_out_to_busy(self, tmp_path, monkeypatch):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"save1")

        await self._assert_timeout_fallthrough(
            svc,
            monkeypatch,
            "SYNC_ALL_GATE_TIMEOUT",
            svc.sync_all_saves,
            {
                "success": False,
                "reason": "sync_busy",
                "message": "Another save-sync run is in progress",
                "synced": 0,
                "conflicts": 0,
                "conflicts_list": [],
                "roms_checked": 0,
                "errors": [],
            },
        )
        assert not any(c[0] in ("upload_save", "list_saves") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_gate_serializes_runs_across_different_roms(self, tmp_path):
        """Two manual syncs on DIFFERENT roms fired concurrently serialize via the gate.

        The per-ROM lock does not serialize different rom_ids — only the device
        gate does. A widening sleep in the stubbed worker would let the two
        overlap if the gate were absent; the gate forces peak concurrency to 1.
        Both runs complete successfully (the second waited, it did not time out).
        """
        svc, _ = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "test-device")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _seed_save_state(
            svc, 2, RomSaveSyncState(system="snes", slot_confirmed=True, active_slot="default"), platform_slug="snes"
        )

        counter_lock = threading.Lock()
        state = {"active": 0, "peak": 0}

        def slow_sync(rom_id, *args):
            with counter_lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            time.sleep(0.05)
            with counter_lock:
                state["active"] -= 1
            return (1, 0, [], [])

        # Every ROM decides via the local matrix (ADR-0017); the device-gate
        # serialization is the same, so stub the matrix worker.
        svc._sync_engine.do_sync_rom_saves = slow_sync  # type: ignore[method-assign]

        results = await asyncio.gather(svc.sync_rom_saves(1), svc.sync_rom_saves(2))

        assert all(r["success"] is True for r in results)
        assert all(r["synced"] == 1 for r in results)
        # Serialized: the device gate never let both worker calls run at once.
        assert state["peak"] == 1
        assert svc._sync_engine._device_gate.is_in_flight() is False


class TestRunRomSyncSession:
    """``_run_rom_sync`` single sync path with a transport-only session (ADR-0017).

    Every ROM decides via the local ``compute_sync_action`` matrix
    (``list_saves`` → ``do_sync_rom_saves``). A confirmed non-legacy ROM
    additionally opens a transport-only ``negotiate`` session around the run —
    its planned operations are ignored; only the ``session_id`` is kept, and the
    session is completed afterward. A legacy ``slot:null`` or not-yet-confirmed
    ROM runs the same matrix with no session wrapper. Any failure opening the
    session degrades to a bare matrix run.
    """

    @pytest.mark.asyncio
    async def test_confirmed_non_legacy_opens_session_and_runs_matrix(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="device-1")
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba")
        _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"local")
        _seed_save_state(svc, 42, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))

        uploaded, downloaded, errors, conflicts = await svc._sync_engine._run_rom_sync(42)

        assert uploaded == 1  # local-only save → matrix POST upload
        assert downloaded == 0
        assert errors == []
        assert conflicts == []
        # The confirmed ROM opened a transport session AND decided via the matrix.
        assert any(c[0] == "negotiate_sync" for c in fake.call_log)
        assert any(c[0] == "list_saves" for c in fake.call_log)
        # The single-ROM trigger completed its own session.
        assert any(c[0] == "complete_sync_session" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_unconfirmed_rom_runs_matrix_without_session(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="device-1")
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba")
        _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"local")
        st = RomSaveSyncState(system="gba")
        st.switch_active_slot("default")  # active_slot set, slot_confirmed stays False
        _seed_save_state(svc, 42, st)

        await svc._sync_engine._run_rom_sync(42)

        assert any(c[0] == "list_saves" for c in fake.call_log)
        # Unconfirmed → no transport session wrapper.
        assert not any(c[0] == "negotiate_sync" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_session_open_failure_still_syncs_without_session(self, tmp_path):
        """A negotiate failure while opening the session degrades to a bare matrix run —
        the sync still happens, just without a session envelope."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="device-1")
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba")
        _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"local")
        _seed_save_state(svc, 42, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        # The session-open negotiate POST raises; the run proceeds without a session.
        fake.fail_on_next(RommConnectionError("negotiate down"))

        uploaded, downloaded, errors, conflicts = await svc._sync_engine._run_rom_sync(42)

        # Session open was attempted and failed; the matrix still ran and POSTed.
        assert any(c[0] == "negotiate_sync" for c in fake.call_log)
        assert any(c[0] == "list_saves" for c in fake.call_log)
        # No session was opened, so none was completed.
        assert not any(c[0] == "complete_sync_session" for c in fake.call_log)
        assert uploaded == 1  # matrix POST upload
        assert downloaded == 0
        assert errors == []
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_confirmed_non_legacy_cross_device_download(self, tmp_path):
        """A confirmed non-legacy ROM with NO local file downloads a server save
        another device made — the matrix pulls it under the transport session."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="device-1")
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba")
        # No local save created.
        _seed_save_state(svc, 42, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        # A server save in the slot this device has never synced (no device_syncs).
        fake.saves[888] = _server_save_with_syncs(
            save_id=888, rom_id=42, filename="pokemon.srm", slot="default", device_syncs=[]
        )
        fake.set_server_save_content(888, b"other device save")

        uploaded, downloaded, errors, conflicts = await svc._sync_engine._run_rom_sync(42)

        # The transport session opened and the matrix drove the download.
        assert any(c[0] == "negotiate_sync" for c in fake.call_log)
        assert uploaded == 0
        assert downloaded == 1
        assert errors == []
        assert conflicts == []
        local = tmp_path / "saves" / "gba" / "pokemon.srm"
        assert local.exists()
        assert local.read_bytes() == b"other device save"

    @pytest.mark.asyncio
    async def test_session_completed_with_counts(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="device-1")
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba")
        _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"local")
        _seed_save_state(svc, 42, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        # Only the session id is consumed from negotiate — its operations are ignored.
        fake.stage_negotiate([], session_id=77)

        await svc._sync_engine._run_rom_sync(42)

        complete = [c for c in fake.call_log if c[0] == "complete_sync_session"]
        assert len(complete) == 1
        assert complete[0][1][0] == 77  # the negotiated session id
        # The matrix POSTed the local save → one completed op, none failed.
        assert complete[0][2]["operations_completed"] == 1
        assert complete[0][2]["operations_failed"] == 0

    @pytest.mark.asyncio
    async def test_malformed_negotiate_response_syncs_without_session(self, tmp_path):
        """A 200 negotiate body missing session_id degrades to a bare matrix run —
        the sync still happens; no session is opened or completed."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="device-1")
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba")
        _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"local")
        _seed_save_state(svc, 42, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))

        def malformed(device_id, saves):
            return {}  # 200 body missing session_id

        fake.negotiate_sync = malformed  # type: ignore[method-assign]

        uploaded, downloaded, errors, conflicts = await svc._sync_engine._run_rom_sync(42)

        # The matrix ran and POSTed; no session was completed (none opened).
        assert any(c[0] == "list_saves" for c in fake.call_log)
        assert not any(c[0] == "complete_sync_session" for c in fake.call_log)
        assert uploaded == 1
        assert downloaded == 0
        assert errors == []
        assert conflicts == []

    @pytest.mark.asyncio
    async def test_confirmed_upload_persists_baseline(self, tmp_path):
        """A confirmed-ROM matrix upload writes the per-file baseline to the PERSISTED
        RomSaveSyncState (launch-gate + slot-switch read it back, not the in-memory ref)."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="device-1")
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba")
        _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"local save")
        _seed_save_state(svc, 42, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))

        await svc._sync_engine._run_rom_sync(42)

        file_state = _require_save_state(svc, 42).files["pokemon.srm"]
        assert file_state.tracked_save_id is not None
        assert file_state.last_sync_hash

    @pytest.mark.asyncio
    async def test_confirmed_download_persists_baseline(self, tmp_path):
        """A confirmed-ROM matrix download writes the per-file baseline (server save
        id + hash) to the PERSISTED RomSaveSyncState."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc, device_id="device-1")
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="pokemon.gba")
        _seed_save_state(svc, 42, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        # Server-only save this device has never synced (no device_syncs) → download.
        fake.saves[888] = _server_save_with_syncs(
            save_id=888, rom_id=42, filename="pokemon.srm", slot="default", device_syncs=[]
        )
        fake.set_server_save_content(888, b"server save bytes")

        await svc._sync_engine._run_rom_sync(42)

        file_state = _require_save_state(svc, 42).files["pokemon.srm"]
        assert file_state.last_sync_server_save_id == 888
        assert file_state.last_sync_hash


class TestSyncAllSavesNegotiate:
    """``sync_all_saves`` whole-device transport session (ADR-0017).

    One transport-only negotiate session wraps the whole sweep; every ROM —
    confirmed non-legacy or legacy ``slot:null`` — decides via the local
    ``list_saves`` matrix. The single session is completed once after the loop.
    When the bulk session can't open, each confirmed non-legacy ROM opens its own.
    """

    @pytest.mark.asyncio
    async def test_one_negotiate_session_for_all_non_legacy_roms(self, tmp_path):
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "device-1")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _seed_save_state(
            svc, 2, RomSaveSyncState(system="snes", slot_confirmed=True, active_slot="default"), platform_slug="snes"
        )
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"s1")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"s2")
        # Only the session id is consumed; both ROMs POST via the matrix.
        fake.stage_negotiate([], session_id=55)

        result = await svc.sync_all_saves()

        assert result["synced"] == 2
        # Exactly ONE whole-device transport session for both ROMs.
        assert len([c for c in fake.call_log if c[0] == "negotiate_sync"]) == 1
        complete = [c for c in fake.call_log if c[0] == "complete_sync_session"]
        assert len(complete) == 1
        assert complete[0][1][0] == 55
        assert complete[0][2]["operations_completed"] == 2
        assert complete[0][2]["operations_failed"] == 0

    @pytest.mark.asyncio
    async def test_legacy_rom_syncs_via_list_saves_in_bulk(self, tmp_path):
        """A legacy slot:null ROM in the bulk run decides via list_saves, under the
        single transport session opened for the confirmed non-legacy ROM."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "device-1")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _seed_save_state(
            svc,
            2,
            RomSaveSyncState(
                system="snes",
                slot_confirmed=True,
                active_slot=None,
                slots={"": {"source": "local", "count": 0, "latest_updated_at": None}},
            ),
            platform_slug="snes",
        )
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"s1")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"s2")

        result = await svc.sync_all_saves()

        # Both ROMs POST via the matrix; one whole-device transport session wraps them.
        assert result["synced"] == 2
        assert len([c for c in fake.call_log if c[0] == "negotiate_sync"]) == 1
        assert any(c[0] == "list_saves" and c[1][0] == 2 for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_cross_device_download_via_bulk(self, tmp_path):
        """Under the shared bulk transport session, a confirmed non-legacy ROM with
        no local file downloads its server save via the matrix (cross-device)."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "device-1")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _seed_save_state(
            svc, 2, RomSaveSyncState(system="snes", slot_confirmed=True, active_slot="default"), platform_slug="snes"
        )
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"s1")  # rom1 local → uploads
        # rom2 has no local file but a server save it has never synced → download.
        fake.saves[900] = _server_save_with_syncs(
            save_id=900, rom_id=2, filename="game2.srm", slot="default", device_syncs=[]
        )
        fake.set_server_save_content(900, b"server save for game2")

        result = await svc.sync_all_saves()

        # rom1 upload + rom2 cross-device download, under one transport session.
        assert result["synced"] == 2
        assert len([c for c in fake.call_log if c[0] == "negotiate_sync"]) == 1
        assert any(c[0] == "download_save_content" and c[1][0] == 900 for c in fake.call_log)
        local = tmp_path / "saves" / "snes" / "game2.srm"
        assert local.exists()
        assert local.read_bytes() == b"server save for game2"

    @pytest.mark.asyncio
    async def test_negotiate_failure_all_roms_fall_back(self, tmp_path):
        """Bulk negotiate failure → session_id None → no bulk session; each confirmed
        ROM opens its own (which fails too) and still syncs via the matrix."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "device-1")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"s1")

        def always_fail(device_id, saves):
            raise RommConnectionError("negotiate down")

        fake.negotiate_sync = always_fail  # type: ignore[method-assign]

        result = await svc.sync_all_saves()

        # No session ever opened → none completed; the matrix still POSTed.
        assert any(c[0] == "list_saves" for c in fake.call_log)
        assert not any(c[0] == "complete_sync_session" for c in fake.call_log)
        assert result["synced"] == 1  # matrix POST upload

    @pytest.mark.asyncio
    async def test_empty_inventory_no_negotiate_no_session(self, tmp_path):
        """No confirmed non-legacy ROM with local saves → empty inventory → the bulk
        negotiate is skipped and no session opens. A legacy ROM still syncs."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "device-1")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _seed_save_state(
            svc,
            1,
            RomSaveSyncState(
                system="gba",
                slot_confirmed=True,
                active_slot=None,
                slots={"": {"source": "local", "count": 0, "latest_updated_at": None}},
            ),
        )
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"s1")

        result = await svc.sync_all_saves()

        assert not any(c[0] == "negotiate_sync" for c in fake.call_log)
        assert not any(c[0] == "complete_sync_session" for c in fake.call_log)
        assert any(c[0] == "list_saves" for c in fake.call_log)
        assert result["synced"] == 1

    @pytest.mark.asyncio
    async def test_complete_failure_is_non_fatal(self, tmp_path):
        """A failing complete_sync_session does not fail the sweep."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "device-1")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"s1")
        fake.complete_raises = RommApiError("complete failed")

        result = await svc.sync_all_saves()

        assert result["success"] is True
        assert result["synced"] == 1
        assert any(c[0] == "complete_sync_session" for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_malformed_negotiate_response_falls_back(self, tmp_path):
        """A malformed bulk negotiate response degrades like a failure — session_id
        None, no session completed, every ROM still syncs via the matrix."""
        svc, fake = make_service(tmp_path)
        svc._config.settings["save_sync_enabled"] = True
        _set_device_id(svc, "device-1")
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _seed_save_state(svc, 1, RomSaveSyncState(system="gba", slot_confirmed=True, active_slot="default"))
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"s1")

        def malformed(device_id, saves):
            return {}  # 200 body missing session_id / operations

        fake.negotiate_sync = malformed  # type: ignore[method-assign]

        result = await svc.sync_all_saves()

        # No session opened/completed; the ROM still synced via the matrix.
        assert not any(c[0] == "complete_sync_session" for c in fake.call_log)
        assert any(c[0] == "list_saves" for c in fake.call_log)
        assert result["synced"] == 1
