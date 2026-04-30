"""Phase 2 tests for the Argosy-style sync rewrite.

Covers the new dispatch path through ``compute_sync_action`` in
``_sync_rom_saves`` / ``_get_save_status_io`` plus the three-action
``resolve_sync_conflict`` callable. Per-rom-lock serialization is exercised
end-to-end via concurrent ``sync_rom_saves`` calls.

These tests use the fixtures from ``test_saves`` (``make_service``,
``_install_rom``, ``_create_save``, ``_server_save``) so failures here surface
the same way as the existing TestSyncRomSaves block.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from .test_saves import (
    _create_save,
    _file_md5,
    _install_rom,
    _server_save,
    make_service,
)

# ---------------------------------------------------------------------------
# Helpers — Argosy fixtures
# ---------------------------------------------------------------------------


def _enable_sync_with_device(svc, device_id: str = "device-1") -> None:
    """Flip on save sync and bind a server device id (matches FakeSaveApi)."""
    svc._save_sync_state["settings"]["save_sync_enabled"] = True
    svc._save_sync_state["device_id"] = device_id
    svc._save_sync_state["server_device_id"] = device_id


def _server_save_with_syncs(
    *,
    save_id: int = 100,
    rom_id: int = 42,
    filename: str = "pokemon.srm",
    updated_at: str = "2026-02-17T06:00:00Z",
    file_size_bytes: int = 1024,
    device_syncs: list[dict] | None = None,
    slot: str | None = None,
) -> dict:
    """Build a server-save dict with explicit device_syncs (no FakeApi shimming)."""
    base = _server_save(
        save_id=save_id,
        rom_id=rom_id,
        filename=filename,
        updated_at=updated_at,
        file_size_bytes=file_size_bytes,
    )
    if slot is not None:
        base["slot"] = slot
    base["device_syncs"] = device_syncs if device_syncs is not None else []
    return base


# ---------------------------------------------------------------------------
# 1a. _sync_rom_saves dispatch
# ---------------------------------------------------------------------------


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

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": ss["updated_at"],
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

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

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # POST → save_id is None
        assert upload_calls[0][2]["save_id"] is None

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] is not None
        assert file_state["last_sync_hash"]

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

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        # Download_save_content was called against the server save id.
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert len(download_calls) == 1
        assert download_calls[0][1][0] == 100

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 100
        assert file_state["last_sync_hash"]  # updated to downloaded content's hash

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

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "deadbeef" * 4,  # baseline differs from current local
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

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

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        saves_dir = tmp_path / "saves" / "gba"
        assert (saves_dir / "pokemon.srm").exists()

    def test_sync_rom_saves_upload_put_when_local_diverged(self, tmp_path):
        """is_current=true + local hash diverges from baseline → Upload (PUT)
        against the existing tracked save id (Row 9)."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        save_path = _create_save(tmp_path, content=b"diverged offline")
        local_hash = _file_md5(str(save_path))

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "0" * 32,  # baseline differs from current local
                    "last_sync_server_updated_at": ss["updated_at"],
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 1
        assert errors == []
        assert conflicts == []
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT — save_id is the existing server save id
        assert upload_calls[0][2]["save_id"] == 100

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["last_sync_hash"] == local_hash

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
        svc._save_sync_state["saves"]["42"] = {"files": {}}

        synced, errors, conflicts = svc._sync_rom_saves(42)

        assert synced == 0
        assert errors == []
        assert conflicts == []
        # No I/O initiated.
        assert not any(c[0] in ("upload_save", "download_save_content", "download_save") for c in fake.call_log)
        # Baseline now persisted.
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["last_sync_hash"] == local_hash

    def test_sync_rom_saves_recovery_download_when_no_local(self, tmp_path):
        """Row 4 — is_current=true on the picked save but local file is gone →
        Download to recover."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # No _create_save here — local file is absent.

        ss = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": True}],
        )
        fake.saves[100] = ss

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "abc",
                    "last_sync_server_updated_at": ss["updated_at"],
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

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
        # via Row 9 (is_current=true + diverged hash).
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "0" * 32,
                    "last_sync_server_updated_at": ss["updated_at"],
                }
            }
        }

        synced, errors, conflicts = svc._sync_rom_saves(42)

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

        before = svc._save_sync_state["saves"].get("42", {}).get("last_sync_check_at")
        assert before is None

        svc._sync_rom_saves(42)

        after = svc._save_sync_state["saves"]["42"]["last_sync_check_at"]
        assert after is not None and isinstance(after, str)


# ---------------------------------------------------------------------------
# 1b. _get_save_status_io parity
# ---------------------------------------------------------------------------


class TestGetSaveStatusArgosy:
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

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "0" * 32,
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }

        result = svc._get_save_status_io(42, [ss])

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
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": ss_skip["updated_at"],
                }
            }
        }
        result_skip = svc._get_save_status_io(42, [ss_skip])
        assert result_skip["files"][0]["status"] == "synced"

        # ---------- Upload ----------
        # Reset state for next case: no server saves
        svc._save_sync_state["saves"]["42"] = {"files": {}}
        result_upload = svc._get_save_status_io(42, [])
        assert result_upload["files"][0]["status"] == "upload"

        # ---------- Download ----------
        # Server moved past us, local matches baseline → Download
        ss_dl = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss_dl
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": local_hash,
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }
        result_dl = svc._get_save_status_io(42, [ss_dl])
        assert result_dl["files"][0]["status"] == "download"

        # ---------- Conflict ----------
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "0" * 32,
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }
        result_conflict = svc._get_save_status_io(42, [ss_dl])
        assert result_conflict["files"][0]["status"] == "conflict"


# ---------------------------------------------------------------------------
# 1c. resolve_sync_conflict
# ---------------------------------------------------------------------------


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

        # Seed a deferred record so we can verify it gets cleared.
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "deferred": {"server_save_id": 100, "server_updated_at": ss["updated_at"]},
                }
            }
        }

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="keep_local")

        assert result["success"] is True
        assert result["action"] == "keep_local"
        assert not any(c[0] == "upload_save" for c in fake.call_log)

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 100
        assert file_state["last_sync_hash"] == local_hash
        assert "deferred" not in file_state

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

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "deferred": {"server_save_id": 100, "server_updated_at": ss["updated_at"]},
                }
            }
        }

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="keep_local")

        assert result["success"] is True
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert len(upload_calls) == 1
        # PUT — save_id was passed
        assert upload_calls[0][2]["save_id"] == 100

        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["last_sync_hash"] == local_hash
        assert "deferred" not in file_state

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

        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "deferred": {"server_save_id": 100, "server_updated_at": ss["updated_at"]},
                }
            }
        }

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="use_server")

        assert result["success"] is True
        # Local file overwritten with server content
        assert save_path.read_bytes() == b"server-truth"
        file_state = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]
        assert file_state["tracked_save_id"] == 100
        assert file_state["last_sync_hash"] == _file_md5(str(save_path))
        assert "deferred" not in file_state

    @pytest.mark.asyncio
    async def test_resolve_defer_persists_deferred_record(self, tmp_path):
        """defer writes a deferred record with the required keys; no I/O."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        # Need a Conflict-shaped state so H2-light doesn't warn on this call.
        save_path = _create_save(tmp_path, content=b"diverged")
        ss = _server_save_with_syncs(
            updated_at="2026-03-15T00:00:00Z",
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss
        svc._save_sync_state["saves"]["42"] = {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_hash": "0" * 32,
                    "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                }
            }
        }

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="defer")

        assert result["success"] is True
        assert result["action"] == "defer"
        # No I/O initiated.
        assert not any(c[0] in ("upload_save", "download_save_content", "download_save") for c in fake.call_log)
        deferred = svc._save_sync_state["saves"]["42"]["files"]["pokemon.srm"]["deferred"]
        assert deferred["server_save_id"] == 100
        assert deferred["server_updated_at"] == "2026-03-15T00:00:00Z"
        assert "deferred_at" in deferred
        # Local untouched
        assert save_path.read_bytes() == b"diverged"

    @pytest.mark.asyncio
    async def test_resolve_invalid_action_returns_error(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="foo")

        assert result["success"] is False
        assert "invalid" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_resolve_rom_not_installed(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)

        result = await svc.resolve_sync_conflict(rom_id=999, filename="pokemon.srm", action="keep_local")

        assert result["success"] is False
        assert result["message"]

    @pytest.mark.asyncio
    async def test_resolve_server_fetch_failure(self, tmp_path):
        """When list_saves raises, return failure without mutating state."""
        from lib.errors import RommApiError

        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"x")

        # Pre-populate state to assert it stays untouched.
        original_state = {
            "files": {
                "pokemon.srm": {"tracked_save_id": 100, "last_sync_hash": "abc"},
            }
        }
        svc._save_sync_state["saves"]["42"] = original_state

        fake.fail_on_next(RommApiError("network"))

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="keep_local")

        assert result["success"] is False
        assert "Failed to fetch saves" in result["message"]
        # State left as-is — no mutation
        assert svc._save_sync_state["saves"]["42"] == original_state

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

        result = await svc.resolve_sync_conflict(rom_id=42, filename="pokemon.srm", action="keep_local")

        assert result["success"] is False
        assert "no server save" in result["message"].lower()


# ---------------------------------------------------------------------------
# 1d. Per-rom lock serialization
# ---------------------------------------------------------------------------


class TestPerRomLockSerialization:
    @pytest.mark.asyncio
    async def test_per_rom_lock_serializes_concurrent_sync(self, tmp_path):
        """Two concurrent sync_rom_saves calls on the same rom must not interleave."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _create_save(tmp_path, content=b"local data")

        # Spy timing on _sync_rom_saves entry/exit. The lock is held in the
        # async wrapper around run_in_executor, so the inner call's
        # entry/exit windows for two concurrent invocations must not overlap.
        events: list[tuple[str, float]] = []
        original = svc._sync_rom_saves

        def wrapped(rom_id: int):
            events.append(("enter", time.time()))
            # Sleep to ensure overlap is *possible* if the lock is broken.
            time.sleep(0.05)
            try:
                return original(rom_id)
            finally:
                events.append(("exit", time.time()))

        svc._sync_rom_saves = wrapped  # type: ignore[method-assign]

        await asyncio.gather(svc.sync_rom_saves(42), svc.sync_rom_saves(42))

        # Expect strictly serialized: enter, exit, enter, exit.
        kinds = [k for k, _ts in events]
        assert kinds == ["enter", "exit", "enter", "exit"], events

    @pytest.mark.asyncio
    async def test_per_rom_lock_does_not_block_different_rom_ids(self, tmp_path):
        """Concurrent syncs on different rom_ids run in parallel."""
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="game1.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="snes", file_name="game2.sfc")
        _create_save(tmp_path, system="gba", rom_name="game1", content=b"a")
        _create_save(tmp_path, system="snes", rom_name="game2", content=b"b")

        events: list[tuple[int, str, float]] = []
        original = svc._sync_rom_saves

        def wrapped(rom_id: int):
            events.append((rom_id, "enter", time.time()))
            time.sleep(0.05)
            try:
                return original(rom_id)
            finally:
                events.append((rom_id, "exit", time.time()))

        svc._sync_rom_saves = wrapped  # type: ignore[method-assign]

        await asyncio.gather(svc.sync_rom_saves(1), svc.sync_rom_saves(2))

        # Both enters must happen before either exit (proves overlap).
        order = [(rid, kind) for rid, kind, _ts in events]
        enters = [i for i, e in enumerate(order) if e[1] == "enter"]
        exits = [i for i, e in enumerate(order) if e[1] == "exit"]
        assert min(exits) > max(enters), order
