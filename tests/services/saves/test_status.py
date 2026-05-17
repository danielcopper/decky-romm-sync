"""Tests for StatusService — save-status DTO building and read-only status checks."""

import pytest

from domain.save_state import RomSaveState
from services.saves.status.builders import _resolve_chosen_server, _status_from_action
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _file_md5,
    _install_rom,
    _server_save,
    _server_save_with_syncs,
    make_service,
)


class TestSaveStatus:
    @pytest.mark.asyncio
    async def test_get_save_status(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.get_save_status(42)
        assert result["rom_id"] == 42
        assert len(result["files"]) >= 1

    @pytest.mark.asyncio
    async def test_get_save_status_no_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = await svc.get_save_status(42)
        assert result["rom_id"] == 42
        assert result["files"] == []

    @pytest.mark.asyncio
    async def test_get_save_status_includes_empty_conflicts_when_no_conflict(self, tmp_path):
        """get_save_status response includes conflicts key (empty when no conflicts)."""
        svc, _ = make_service(tmp_path)
        _install_rom(svc, tmp_path)

        result = await svc.get_save_status(42)
        assert "conflicts" in result
        assert result["conflicts"] == []

    @pytest.mark.asyncio
    async def test_get_save_status_includes_device_syncs(self, tmp_path):
        """get_save_status includes device_syncs and is_current per file."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        svc._save_sync_state.server_device_id = "server-dev-1"
        svc._save_sync_state.device_id = "server-dev-1"

        ss = _server_save()
        ss["device_syncs"] = [
            {
                "device_id": "server-dev-1",
                "device_name": "my-deck",
                "is_current": True,
                "last_synced_at": "2026-03-24T10:00:00",
            },
            {
                "device_id": "server-dev-2",
                "device_name": "desktop",
                "is_current": False,
                "last_synced_at": "2026-03-24T08:00:00",
            },
        ]
        fake.saves[100] = ss

        result = await svc.get_save_status(42)
        file_status = result["files"][0]
        assert "device_syncs" in file_status
        assert len(file_status["device_syncs"]) == 2
        assert file_status["device_syncs"][0]["device_name"] == "my-deck"
        assert file_status["is_current"] is True

    @pytest.mark.asyncio
    async def test_save_status_filters_by_active_slot(self, tmp_path):
        """Saves from a different slot should not appear in status."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        # Server save in slot "default", but active_slot is "other"
        ss = _server_save(slot="default")
        fake.saves[100] = ss
        svc._save_sync_state.saves["42"] = RomSaveState(active_slot="other")

        result = await svc.get_save_status(42)
        # Local file exists → should show as upload (local-only), not synced against wrong slot
        assert len(result["files"]) == 1
        assert result["files"][0]["status"] == "upload"
        assert result["files"][0]["server_save_id"] is None


class TestGetSaveStatusComputeAction:
    def test_get_save_status_returns_sync_conflict_shape(self, tmp_path):
        """When compute_sync_action emits Conflict, get_save_status surfaces it."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"diverged local")
        _ = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "0" * 32,
                        "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                    }
                }
            }
        )

        result = svc._status._get_save_status_io(42, [ss])

        assert len(result["conflicts"]) == 1
        c = result["conflicts"][0]
        assert isinstance(c, dict)
        assert c["type"] == "sync_conflict"
        assert c["rom_id"] == 42
        assert c["filename"] == "pokemon.srm"
        assert c["server_save_id"] == 100
        assert "created_at" in c

    def test_get_save_status_status_field_mapping(self, tmp_path):
        """Skip→synced, Upload→upload, Download→download, Conflict→conflict."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        # ---------- Skip ----------
        save_path = _create_save(tmp_path, content=b"matches baseline")
        local_hash = _file_md5(str(save_path))
        ss_skip = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": local_hash,
                        "last_sync_server_updated_at": ss_skip["updated_at"],
                    }
                }
            }
        )
        result_skip = svc._status._get_save_status_io(42, [ss_skip])
        assert result_skip["files"][0]["status"] == "synced"

        # ---------- Upload ----------
        # Reset state for next case: no server saves
        svc._save_sync_state.saves["42"] = RomSaveState()
        result_upload = svc._status._get_save_status_io(42, [])
        assert result_upload["files"][0]["status"] == "upload"

        # ---------- Download ----------
        # Server moved past us, local matches baseline → Download
        ss_dl = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss_dl
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
        result_dl = svc._status._get_save_status_io(42, [ss_dl])
        assert result_dl["files"][0]["status"] == "download"

        # ---------- Conflict ----------
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "0" * 32,
                        "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                    }
                }
            }
        )
        result_conflict = svc._status._get_save_status_io(42, [ss_dl])
        assert result_conflict["files"][0]["status"] == "conflict"

    def test_get_save_status_server_only_collapses_to_one_entry(self, tmp_path):
        """Multiple server saves in the active slot but no local file →
        exactly one entry returned (the newest server save), not one per
        server save. Older versions are reachable via list_file_versions."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # No local file.

        ss_old = _server_save_with_syncs(
            save_id=200,
            updated_at="2026-03-24T10:00:00",
            device_syncs=[{"device_id": "device-other", "is_current": True}],
        )
        ss_new = _server_save_with_syncs(
            save_id=201,
            updated_at="2026-03-24T15:00:00",
            device_syncs=[{"device_id": "device-other", "is_current": True}],
        )
        fake.saves[200] = ss_old
        fake.saves[201] = ss_new

        svc._save_sync_state.saves["42"] = RomSaveState()

        result = svc._status._get_save_status_io(42, [ss_old, ss_new])

        assert len(result["files"]) == 1
        entry = result["files"][0]
        assert entry["server_save_id"] == 201  # newest
        assert entry["status"] == "download"
        assert entry["local_path"] is None

    def test_get_save_status_empty_slot_returns_no_entries(self, tmp_path):
        """No local file and no server saves → files list is empty."""
        svc, _fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        svc._save_sync_state.saves["42"] = RomSaveState()

        result = svc._status._get_save_status_io(42, [])

        assert result["files"] == []
        assert result["conflicts"] == []


class TestSaveSyncDisplayEnrichment:
    """get_save_status ships a pre-computed save_sync_display alongside files/conflicts."""

    def test_empty_slot_display_no_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        svc._save_sync_state.saves["42"] = RomSaveState()

        result = svc._status._get_save_status_io(42, [])

        assert result["save_sync_display"] == {
            "status": "none",
            "label": "No saves",
            "last_sync_check_at": None,
        }

    def test_synced_display_passes_through_check_timestamp(self, tmp_path):
        """Synced state with a recorded sync check passes the ISO through; frontend formats time-ago."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"matches baseline")
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
                },
                "last_sync_check_at": "2026-04-01T08:00:00+00:00",
            }
        )

        result = svc._status._get_save_status_io(42, [ss])

        assert result["save_sync_display"] == {
            "status": "synced",
            "label": None,
            "last_sync_check_at": "2026-04-01T08:00:00+00:00",
        }

    def test_conflict_display(self, tmp_path):
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"diverged local")
        ss = _server_save_with_syncs(device_syncs=[{"device_id": "device-1", "is_current": False}])
        fake.saves[100] = ss
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "0" * 32,
                        "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                    }
                }
            }
        )

        result = svc._status._get_save_status_io(42, [ss])

        assert result["save_sync_display"]["status"] == "conflict"
        assert result["save_sync_display"]["label"] == "Conflict"
        assert result["save_sync_display"]["last_sync_check_at"] is None


class TestBuildersDefensiveBranches:
    """Direct coverage of the defensive fallbacks in builders.py."""

    def test_status_from_action_unknown_type_defaults_to_synced(self):
        """An action that matches none of Skip/Upload/Download/Conflict falls back to "synced"."""
        assert _status_from_action(object()) == "synced"

    def test_resolve_chosen_server_empty_candidates_returns_none(self):
        """Skip-branch with no server candidates yields no chosen server save."""
        # A bare object is not Download/Conflict/Upload, so the function falls
        # through to the candidates check; an empty list returns None.
        assert _resolve_chosen_server(object(), []) is None


class TestServerQueryFailed:
    """``get_save_status`` must surface a connectivity-failure flag and
    suppress the misleading "ready to upload" indicators an empty server
    list would otherwise produce against local-only saves."""

    @pytest.mark.asyncio
    async def test_list_saves_failure_sets_flag_and_marks_status_unknown(self, tmp_path):
        """OSError from list_saves → server_query_failed=True, file status=unknown."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        fake.fail_on_next(OSError("connection reset"))

        result = await svc.get_save_status(42)

        assert result["server_query_failed"] is True
        # A local save exists, so a row still appears — but the misleading
        # matrix-derived "upload" verdict against an empty server list is
        # replaced with the neutral "unknown" status.
        assert len(result["files"]) == 1
        file_row = result["files"][0]
        assert file_row["status"] == "unknown"
        assert file_row["filename"] == "pokemon.srm"
        # Local-side fields stay intact (they come from local state, not
        # from the failed list_saves call).
        assert file_row["local_path"] is not None
        assert file_row["local_hash"] is not None
        assert file_row["local_size"] is not None
        # Server-side attribution is nulled out — we have no server info.
        assert file_row["server_save_id"] is None
        assert file_row["server_file_name"] is None
        assert file_row["server_emulator"] is None
        assert file_row["server_updated_at"] is None
        assert file_row["server_size"] is None
        assert file_row["device_syncs"] == []
        assert file_row["uploaded_by_us"] is None
        # No conflicts can be reported when we don't actually know the
        # server state.
        assert result["conflicts"] == []
        # The aggregate display collapses to a neutral "Server unreachable"
        # label rather than the misleading "Not synced" / "Synced".
        assert result["save_sync_display"] == {
            "status": "none",
            "label": "Server unreachable",
            "last_sync_check_at": None,
        }

    @pytest.mark.asyncio
    async def test_happy_path_flag_is_false(self, tmp_path):
        """Successful list_saves preserves the normal flow with the flag set False."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)

        ss = _server_save()
        fake.saves[100] = ss

        result = await svc.get_save_status(42)

        assert result["server_query_failed"] is False
        assert len(result["files"]) >= 1
        # Real matrix verdict surfaced (not redacted).
        assert result["files"][0]["status"] != "unknown"

    @pytest.mark.asyncio
    async def test_empty_server_response_is_not_failure(self, tmp_path):
        """Genuine empty list (no saves on server) ≠ server_query_failed.

        list_saves returns ``[]`` legitimately (no saves uploaded for this
        ROM yet). The matrix correctly classifies the local-only file as
        "upload" — that's the truth of the situation, not a misleading
        artifact. The flag stays False and the display does NOT collapse
        to "Server unreachable".
        """
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        # No fail_on_next, no fake.saves entry → list_saves returns [].

        result = await svc.get_save_status(42)

        assert result["server_query_failed"] is False
        # The matrix's "upload" verdict on a local-only file with a truly
        # empty server is the correct answer, not a stale-cache artifact.
        assert len(result["files"]) == 1
        assert result["files"][0]["status"] == "upload"
        # save_sync_display is NOT the "Server unreachable" fallback.
        assert result["save_sync_display"]["label"] != "Server unreachable"

    def test_redacted_entry_preserves_local_metadata(self, tmp_path):
        """The bad-path redaction must not strip local-side metadata —
        users still need to see *which* file is in the unknown state."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path)
        ss = _server_save()
        fake.saves[100] = ss

        # Drive the IO helper directly with the failure flag so we exercise
        # the redaction path against a "successful" matrix result.
        result = svc._status._get_save_status_io(42, [ss], server_query_failed=True)

        assert result["server_query_failed"] is True
        assert len(result["files"]) == 1
        row = result["files"][0]
        assert row["status"] == "unknown"
        assert row["filename"] == "pokemon.srm"
        assert row["local_path"] is not None
        assert row["local_size"] == 1024
        assert row["server_save_id"] is None
        assert result["conflicts"] == []


class TestCompositeServerQueryFailedDomain:
    """compute_save_sync_display short-circuits on server_query_failed."""

    def test_failure_flag_overrides_files_and_check_timestamp(self):
        """Even with files and a recent check, the failure flag wins."""
        from domain.save_status import compute_save_sync_display

        files = [{"filename": "pokemon.srm", "status": "synced", "local_path": "/x/pokemon.srm"}]
        display = compute_save_sync_display(
            files,
            "2026-04-01T08:00:00+00:00",
            server_query_failed=True,
        )
        assert display.status == "none"
        assert display.label == "Server unreachable"
        assert display.last_sync_check_at is None

    def test_happy_path_default_kw_unchanged(self):
        """Omitting the kw argument keeps the legacy behavior intact."""
        from domain.save_status import compute_save_sync_display

        files = [{"filename": "pokemon.srm", "status": "synced", "local_path": "/x/pokemon.srm"}]
        display = compute_save_sync_display(files, "2026-04-01T08:00:00+00:00")
        assert display.status == "synced"
        assert display.label is None
        assert display.last_sync_check_at == "2026-04-01T08:00:00+00:00"
