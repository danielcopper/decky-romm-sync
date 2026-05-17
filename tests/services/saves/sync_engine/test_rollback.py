"""Tests for RollbackOrchestrator — conflict-resolution rollback paths. The
``keep_local`` / ``use_server`` decision after a true two-sided sync conflict
commits one side, including server-head freshness checks against the stored
``server_save_id`` to defend against third-device races. Multi-version
timeline rollbacks (older save versions) belong to VersionsService and are
tested under tests/services/saves/test_versions.py.
"""

import os

import pytest

from domain.save_state import FileSyncState, RomSaveState
from lib.errors import RommApiError
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _file_md5,
    _install_rom,
    _server_save_with_syncs,
    make_service,
)


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
