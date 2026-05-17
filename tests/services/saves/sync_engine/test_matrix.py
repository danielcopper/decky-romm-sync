"""Tests for MatrixExecutor — newest-wins matrix evaluation and per-file sync I/O
dispatch. Anything that decides "which side wins for this file" or moves bytes
between local saves_dir and the RomM server lives in
py_modules/services/saves/sync_engine/matrix.py and is exercised here. Public-
callable orchestration (lock acquisition, guards) lives in test_engine.py;
device registration in test_devices.py; conflict rollback in test_rollback.py.
"""

import hashlib
import os

import pytest

from domain.save_state import RomSaveState
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
    """Tests for MatrixExecutor.update_file_sync_state, the per-file sync-state writer."""

    def test_creates_proper_entry(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._sync_engine._matrix.update_file_sync_state("42", "pokemon.srm", server_resp, str(save_file), "gba")

        entry = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert entry.last_sync_hash == svc._save_file.checksum_md5(str(save_file))
        assert entry.last_sync_at is not None
        assert entry.last_sync_server_save_id == 200

    def test_creates_entry_with_new_fields(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path)
        server_resp = {"id": 200, "updated_at": "2026-02-17T15:00:00Z"}

        svc._sync_engine._matrix.update_file_sync_state(
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

        svc._sync_engine._matrix.update_file_sync_state(
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

        svc._sync_engine._matrix.update_file_sync_state(
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

        svc._sync_engine._matrix.update_file_sync_state("42", "pokemon.srm", server_response, local_path, "gba")

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert isinstance(file_state.last_sync_local_mtime, float)
        assert file_state.last_sync_local_mtime == pytest.approx(os.path.getmtime(local_path))

    def test_writes_last_sync_local_size_as_int(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path, system="gba", rom_name="pokemon", content=b"\x00" * 2048)
        local_path = str(save_file)
        server_response = _server_save()

        svc._sync_engine._matrix.update_file_sync_state("42", "pokemon.srm", server_response, local_path, "gba")

        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert isinstance(file_state.last_sync_local_size, int)
        assert file_state.last_sync_local_size == 2048

    def test_does_not_write_old_local_mtime_at_last_sync_key(self, tmp_path):
        svc, _ = make_service(tmp_path)
        save_file = _create_save(tmp_path, system="gba", rom_name="pokemon")
        local_path = str(save_file)
        server_response = _server_save()

        svc._sync_engine._matrix.update_file_sync_state("42", "pokemon.srm", server_response, local_path, "gba")

        # The legacy field name was never part of FileSyncState; ensure the
        # serialised on-disk shape doesn't carry it either.
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert "local_mtime_at_last_sync" not in file_state.to_dict()

    def test_writes_none_for_missing_file(self, tmp_path):
        svc, _ = make_service(tmp_path)
        local_path = str(tmp_path / "saves" / "gba" / "missing.srm")
        server_response = _server_save()

        svc._sync_engine._matrix.update_file_sync_state("42", "missing.srm", server_response, local_path, "gba")

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
    written by ``update_file_sync_state`` never reaches disk on that path —
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
