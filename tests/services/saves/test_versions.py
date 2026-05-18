"""Tests for VersionsService — save version history reads and rollback flow."""

import pytest

from domain.save_state import RomSaveState
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _file_md5,
    _install_rom,
    _server_save,
    _server_save_with_syncs,
    make_service,
)


class TestListFileVersions:
    """Tests for SaveService.list_file_versions."""

    def _setup_state(self, svc, tracked_id: int | None) -> None:
        """Populate save state with a tracked save id for rom 42, pokemon.srm."""
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "files": {
                    "pokemon.srm": {"tracked_save_id": tracked_id},
                },
            }
        )

    @pytest.mark.asyncio
    async def test_happy_path_excludes_tracked(self, tmp_path):
        """Returns every save in the slot except the currently-tracked one.

        The slot is the unit, not the filename — saves with different
        ``file_name_no_tags`` (uploaded by RomM web UI or third-party
        clients with their own naming) are first-class versions and
        surface alongside saves whose names match the local filename.
        """
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-01T10:00:00Z")
        # Foreign-client upload with a different basename — still a valid
        # version of the same slot, must appear in the version list.
        fake.saves[60] = {
            "id": 60,
            "rom_id": 42,
            "file_name": "other.srm",
            "file_name_no_tags": "other",
            "file_extension": "srm",
            "updated_at": "2026-03-05T10:00:00Z",
            "file_size_bytes": 512,
            "slot": "default",
            "download_path": "/saves/other.srm",
        }
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result["status"] == "ok"
        ids_in_order = [v["id"] for v in result["versions"]]
        assert ids_in_order == [60, 50]  # newest first; tracked id=100 excluded

    @pytest.mark.asyncio
    async def test_foreign_tracked_save_does_not_hide_local_versions(self, tmp_path):
        """When the tracked save was uploaded by another client with a
        different basename, legitimate versions of the local file still
        surface — they are no longer filtered against the tracked save's
        ``file_name_no_tags``.
        """
        svc, fake = make_service(tmp_path)

        # Tracked save with a foreign naming scheme.
        fake.saves[100] = _server_save(
            save_id=100,
            rom_id=42,
            slot="default",
            updated_at="2026-03-10T10:00:00Z",
            filename="alt_name [2026-03-10_10-00-00].srm",
            file_name_no_tags="alt_name",
        )
        fake.saves[50] = _server_save(
            save_id=50,
            rom_id=42,
            slot="default",
            updated_at="2026-03-01T10:00:00Z",
            filename="pokemon [2026-03-01_10-00-00].srm",
            file_name_no_tags="pokemon",
        )
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result["status"] == "ok"
        assert len(result["versions"]) == 1
        assert result["versions"][0]["id"] == 50

    @pytest.mark.asyncio
    async def test_sorted_newest_first(self, tmp_path):
        """Versions are sorted by updated_at descending."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[30] = _server_save(save_id=30, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-01T10:00:00Z")
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result["status"] == "ok"
        versions = result["versions"]
        assert len(versions) == 2
        assert versions[0]["id"] == 50  # newer of the two old versions first
        assert versions[1]["id"] == 30

    @pytest.mark.asyncio
    async def test_empty_when_no_older_versions(self, tmp_path):
        """Returns empty list when there are no versions other than the tracked one."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result == {"status": "ok", "versions": []}

    @pytest.mark.asyncio
    async def test_no_tracked_save_returns_every_save_in_slot(self, tmp_path):
        """Without ``tracked_save_id`` in state, every save in the slot is
        returned — there is no tracked save to exclude."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-01T10:00:00Z")
        # No state at all (tracked_id is None)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result["status"] == "ok"
        assert {v["id"] for v in result["versions"]} == {100, 50}

    @pytest.mark.asyncio
    async def test_api_error_returns_server_unreachable(self, tmp_path):
        """Server failure returns a distinct ``server_unreachable`` status.

        Critical: do NOT return a bare empty list — the frontend would
        render that as "no older versions" when actually the server is
        unreachable. The discriminated status lets the UI show a retry
        affordance instead.
        """
        svc, fake = make_service(tmp_path)

        fake.fail_on_next(OSError("network error"))

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result["status"] == "server_unreachable"
        assert "network" in result["error"].lower()
        # Must NOT be the same shape as the happy-path empty case.
        assert result != {"status": "ok", "versions": []}

    @pytest.mark.asyncio
    async def test_result_shape(self, tmp_path):
        """Each entry contains the required fields: id, updated_at, file_size_bytes, device_syncs."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = {
            "id": 50,
            "rom_id": 42,
            "file_name": "pokemon [2026-03-01_10-00-00].srm",
            "file_name_no_tags": "pokemon",
            "emulator": "retroarch-mgba",
            "updated_at": "2026-03-01T10:00:00Z",
            "file_size_bytes": 2048,
            "device_syncs": [
                {"device_id": "abc", "device_name": "steamdeck", "is_current": True, "last_synced_at": None}
            ],
            "slot": "default",
            "download_path": "/saves/pokemon.srm",
        }
        self._setup_state(svc, tracked_id=100)

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result["status"] == "ok"
        versions = result["versions"]
        assert len(versions) == 1
        entry = versions[0]
        assert entry["id"] == 50
        assert entry["file_name"] == "pokemon [2026-03-01_10-00-00].srm"
        assert entry["emulator"] == "retroarch-mgba"
        assert entry["updated_at"] == "2026-03-01T10:00:00Z"
        assert entry["file_size_bytes"] == 2048
        assert len(entry["device_syncs"]) == 1
        assert entry["device_syncs"][0]["device_name"] == "steamdeck"

    @pytest.mark.asyncio
    async def test_list_file_versions_populates_uploaded_by_us(self, tmp_path):
        """uploaded_by_us is True for IDs in own_upload_ids, False for others."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-05T10:00:00Z")
        fake.saves[30] = _server_save(save_id=30, rom_id=42, slot="default", updated_at="2026-03-01T10:00:00Z")

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "own_upload_ids": [50],
                "files": {
                    "pokemon.srm": {"tracked_save_id": 100},
                },
            }
        )

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result["status"] == "ok"
        versions = result["versions"]
        assert len(versions) == 2
        by_id = {v["id"]: v for v in versions}
        assert by_id[50]["uploaded_by_us"] is True
        assert by_id[30]["uploaded_by_us"] is False

    @pytest.mark.asyncio
    async def test_list_file_versions_legacy_state_returns_none(self, tmp_path):
        """When rom state has no own_upload_ids key, uploaded_by_us is None for all versions."""
        svc, fake = make_service(tmp_path)

        fake.saves[100] = _server_save(save_id=100, rom_id=42, slot="default", updated_at="2026-03-10T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-03-05T10:00:00Z")

        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                # own_upload_ids key intentionally absent — legacy state
                "files": {
                    "pokemon.srm": {"tracked_save_id": 100},
                },
            }
        )

        result = await svc.list_file_versions(42, "default", "pokemon.srm")

        assert result["status"] == "ok"
        versions = result["versions"]
        assert len(versions) == 1
        assert versions[0]["uploaded_by_us"] is None


class TestRollbackToVersion:
    """Tests for SaveService.rollback_to_version — the core rollback flow."""

    def _setup_state(self, svc, tmp_path, tracked_id: int, last_sync_hash: str | None = None) -> None:
        _install_rom(svc, tmp_path)
        _enable_sync_with_device(svc)
        svc._save_sync_state.saves["42"] = RomSaveState.from_dict(
            {
                "system": "gba",
                "active_slot": "default",
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": tracked_id,
                        "last_sync_hash": last_sync_hash,
                    },
                },
            }
        )

    @staticmethod
    def _tracked_save(save_id: int, *, updated_at: str = "2026-03-10T10:00:00Z") -> dict:
        """Build a tracked-save fixture with our device flagged ``is_current``
        on it. Use this for tests where the matrix pre-flight should return
        ``Skip(synced)`` so the switch flow itself is what's exercised.
        """
        return _server_save_with_syncs(
            save_id=save_id,
            slot="default",
            updated_at=updated_at,
            device_syncs=[{"device_id": "device-1", "is_current": True, "last_synced_at": updated_at}],
        )

    @pytest.mark.asyncio
    async def test_returns_rom_not_installed_when_rom_uninstalled(self, tmp_path):
        """Returns rom_not_installed (NOT version_deleted) when the ROM
        isn't in ``installed_roms``.

        The two cases used to collide on ``not_found``; the frontend then
        told the user the version was gone from the server when in fact
        the local ROM install was what disappeared. This test pins the
        split so a regression to the conflated status would fail.
        """
        svc, _fake = make_service(tmp_path)

        # rom 999 is not installed
        result = await svc.rollback_to_version(999, "default", 50)
        assert result == {"status": "rom_not_installed"}
        # Regression guard: must NOT collide with the genuinely-deleted case.
        assert result["status"] != "version_deleted"

    @pytest.mark.asyncio
    async def test_returns_version_deleted_when_save_id_missing(self, tmp_path):
        """Returns version_deleted when target save_id is not in the server response."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        # Request save_id=999, which doesn't exist
        result = await svc.rollback_to_version(42, "default", 999)
        assert result == {"status": "version_deleted"}

    @pytest.mark.asyncio
    async def test_proceeds_even_with_newer_foreign_save(self, tmp_path):
        """Switch is not blocked when a newer foreign save exists in the slot.

        Gate E was removed — switching to a previous version is an explicit
        user action. The newer foreign save still gets adopted by the
        pre-flight (matrix returns Download), then the switch proceeds.
        """
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        # newer save from another device — pre-flight adopts it, switch then
        # bumps id=50 above it.
        fake.saves[200] = _server_save(save_id=200, rom_id=42, slot="default", updated_at="2026-03-20T10:00:00Z")
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_tracked_missing_does_not_block(self, tmp_path):
        """Switch proceeds when the currently-tracked save is gone from the
        server. The matrix pre-flight runs against whatever's actually in
        the slot and the user's chosen target survives that run.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        self._setup_state(svc, tmp_path, tracked_id=999)
        _create_save(tmp_path)
        # Tracked save 999 does NOT exist on server. Only save 50 is present
        # (and is the rollback target).
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_unsynced_changes_get_uploaded_before_switch(self, tmp_path):
        """Local diverged from last_sync_hash + we're flagged ``is_current`` on
        the tracked save → matrix returns ``Upload(PUT)`` → pre-flight
        silently pushes local up to the server → switch proceeds. No warning,
        no force flag, no data loss.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash="aabbcc001122334455667788")
        _create_save(tmp_path, content=b"\xff" * 1024)

        fake.saves[100] = _server_save_with_syncs(
            save_id=100,
            slot="default",
            updated_at="2026-03-10T10:00:00Z",
            device_syncs=[{"device_id": "device-1", "is_current": True, "last_synced_at": "2026-03-10T10:00:00Z"}],
        )
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # Pre-flight PUT against id=100 happened (the silent upload of local
        # changes), then switch PUT against id=50 (the rollback bump).
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        upload_targets = [c[2].get("save_id") for c in upload_calls]
        assert 100 in upload_targets  # pre-flight PUT
        assert 50 in upload_targets  # switch PUT

    @pytest.mark.asyncio
    async def test_unsynced_with_server_moved_blocks_via_conflict(self, tmp_path):
        """Local diverged + we're not ``is_current`` (someone else uploaded) →
        matrix returns ``Conflict`` → switch returns ``conflict_blocked`` and
        does no I/O on the rollback target. The frontend resolves via the
        standard SyncConflictModal.
        """
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash="aabbcc001122334455667788")
        _create_save(tmp_path, content=b"\xff" * 1024)

        fake.saves[100] = _server_save_with_syncs(
            save_id=100,
            slot="default",
            updated_at="2026-03-15T10:00:00Z",
            device_syncs=[{"device_id": "device-1", "is_current": False, "last_synced_at": "2026-03-10T10:00:00Z"}],
        )
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "conflict_blocked"
        assert len(result["conflicts"]) == 1
        # No download of the rollback target should have happened.
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert not any(c[1][0] == 50 for c in download_calls)

    @pytest.mark.asyncio
    async def test_happy_path_downloads_and_updates_state(self, tmp_path):
        """Happy path: rollback downloads the target save and updates file sync state."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        # tracked (no change since last sync)
        fake.saves[100] = self._tracked_save(100)
        # older version to roll back to
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # Verify download was called with save_id=50
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert any(c[1][0] == 50 for c in download_calls)
        # State updated: tracked_save_id should now point to the rolled-back save
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id == 50

    @pytest.mark.asyncio
    async def test_no_local_file_pre_flight_downloads_then_switch_proceeds(self, tmp_path):
        """When local file doesn't exist, the matrix pre-flight pulls the
        server's tracked save (recovery path) before the switch overwrites it
        with the chosen older version."""
        svc, fake = make_service(tmp_path)

        # No local file. Tracked save 100 exists with our device current.
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash="somehash")
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # Both saves were downloaded: 100 from pre-flight recovery, 50 from
        # the actual switch.
        download_ids = [c[1][0] for c in fake.call_log if c[0] == "download_save_content"]
        assert 100 in download_ids
        assert 50 in download_ids

    @pytest.mark.asyncio
    async def test_no_state_entry_proceeds_via_baseline_adoption(self, tmp_path):
        """No ``last_sync_hash`` baseline yet → matrix returns
        ``Skip(adopt_baseline=True)`` for the tracked save → pre-flight is a
        no-op state hygiene write → switch proceeds normally."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path, content=b"\xff" * 1024)
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=None)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_server_error_returns_preflight_failed(self, tmp_path):
        """Server failure during the matrix pre-flight aborts the switch
        with ``preflight_failed`` (the rollback never runs)."""
        svc, fake = make_service(tmp_path)

        _install_rom(svc, tmp_path)
        _enable_sync_with_device(svc)
        fake.fail_on_next(Exception("network error"))

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "preflight_failed"
        assert any("network" in err.lower() for err in result.get("errors", []))

    @pytest.mark.asyncio
    async def test_post_preflight_list_saves_failure_returns_server_unreachable(self, tmp_path):
        """A ``list_saves`` failure AFTER a clean pre-flight returns the
        distinct ``server_unreachable`` status, not ``not_found``.

        Before #627 this case returned ``{"status": "not_found"}`` —
        identical to the "save_id genuinely deleted" case — so the user
        was told "this version is no longer on the server" when in fact
        the server was unreachable. The distinct status lets the
        frontend surface a retry affordance.
        """
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        # Let the pre-flight ``list_saves`` succeed, then fail on the next
        # ``list_saves`` call — the one the post-preflight switch makes.
        original_list = fake.list_saves
        call_count = {"n": 0}

        def fail_second_list(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise OSError("server unreachable after preflight")
            return original_list(*args, **kwargs)

        fake.list_saves = fail_second_list  # type: ignore[method-assign]

        try:
            result = await svc.rollback_to_version(42, "default", 50)
        finally:
            fake.list_saves = original_list  # type: ignore[method-assign]

        assert result["status"] == "server_unreachable"
        assert "unreachable" in result.get("error", "").lower()
        # Critical: must NOT collide with the "genuinely deleted" case.
        assert result["status"] != "version_deleted"

    # ------------------------------------------------------------------
    # Cross-device propagation: rollback re-PUTs target so it becomes
    # newest-in-slot and the rollback target wins on other devices' next sync.
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_rollback_uploads_target_after_download(self, tmp_path):
        """Rollback PUTs the target save_id after downloading so updated_at bumps server-side."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # Verify a PUT (upload_save with save_id=50) fired after download
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert any(c[2].get("save_id") == 50 for c in upload_calls), f"expected PUT to save_id=50, got {upload_calls!r}"

    @pytest.mark.asyncio
    async def test_rollback_calls_confirm_download_on_target(self, tmp_path):
        """Rollback marks our device as current on the target save via confirm_download."""
        svc, fake = make_service(tmp_path)
        # device_id must be set for confirm_download to fire
        svc._save_sync_state.server_device_id = "device-1"

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        confirm_calls = [c for c in fake.call_log if c[0] == "confirm_download"]
        assert any(c[1] == (50, "device-1") for c in confirm_calls), (
            f"expected confirm_download(50, 'device-1'), got {confirm_calls!r}"
        )

    @pytest.mark.asyncio
    async def test_rollback_updates_tracked_id_and_hash(self, tmp_path):
        """After rollback, local state has tracked_save_id=target and last_sync_hash matches downloaded content."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id == 50
        # Hash should match the (re-uploaded) local file content
        local_path = tmp_path / "saves" / "gba" / "pokemon.srm"
        assert file_state.last_sync_hash == _file_md5(str(local_path))
        # last_sync_server_updated_at reflects the post-PUT response (NOT the
        # pre-PUT target.updated_at), confirming the bump propagated locally.
        assert file_state.last_sync_server_updated_at != "2026-02-01T10:00:00Z"

    @pytest.mark.asyncio
    async def test_rollback_returns_put_failed_when_upload_raises(self, tmp_path):
        """When the PUT step fails, status reflects put_failed and download survives.

        Partial-rollback semantics: the local download already mutated state to
        point at the target save_id, so the rollback IS locally complete. We
        persist that state (saved on put_failed) so a restart doesn't leave a
        state/file mismatch. Cross-device propagation is what failed; the user
        can retry rollback_to_version idempotently.
        """
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        # Track call sequence: download must succeed, then upload must fail.
        original_upload = fake.upload_save

        def failing_upload(*args, **kwargs):
            raise Exception("PUT failed: server 500")

        fake.upload_save = failing_upload  # type: ignore[method-assign]

        try:
            result = await svc.rollback_to_version(42, "default", 50)
        finally:
            fake.upload_save = original_upload  # type: ignore[method-assign]

        assert result["status"] == "put_failed"
        assert "PUT failed" in result.get("error", "")
        # Download did happen
        download_calls = [c for c in fake.call_log if c[0] == "download_save_content"]
        assert any(c[1][0] == 50 for c in download_calls)
        # Local state still updated to point at target — save_state was called
        # so disk file and state file remain consistent.
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id == 50

    @pytest.mark.asyncio
    async def test_rollback_ignores_confirm_download_failure(self, tmp_path):
        """confirm_download failure is non-fatal — rollback still reports ok and updates state.

        ``_do_upload_save`` swallows confirm_download errors (debug-logged).
        From the rollback's perspective the PUT succeeded, so cross-device
        propagation works (the next list_saves still picks our bumped
        ``updated_at``). is_current may not be set on our device until the next
        sync, but that's recoverable — the next compute_sync_action will
        detect tracked_save_id==server.id and Skip.
        """
        svc, fake = make_service(tmp_path)
        svc._save_sync_state.server_device_id = "device-1"

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        self._setup_state(svc, tmp_path, tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = self._tracked_save(100)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        original_confirm = fake.confirm_download

        def failing_confirm(*args, **kwargs):
            raise Exception("confirm_download network error")

        fake.confirm_download = failing_confirm  # type: ignore[method-assign]

        try:
            result = await svc.rollback_to_version(42, "default", 50)
        finally:
            fake.confirm_download = original_confirm  # type: ignore[method-assign]

        assert result["status"] == "ok"
        # Upload still happened
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert any(c[2].get("save_id") == 50 for c in upload_calls)
        # State updated
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id == 50

    @pytest.mark.asyncio
    async def test_rollback_to_already_tracked_save_is_idempotent(self, tmp_path):
        """Rolling back to the currently-tracked save still PUTs (idempotent bump)."""
        svc, fake = make_service(tmp_path)

        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        # Tracked save IS the target — rollback to currently-owned save
        self._setup_state(svc, tmp_path, tracked_id=50, last_sync_hash=local_hash)
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="default", updated_at="2026-02-01T10:00:00Z")

        result = await svc.rollback_to_version(42, "default", 50)

        assert result["status"] == "ok"
        # PUT still fired (bumps updated_at, idempotent re-confirm)
        upload_calls = [c for c in fake.call_log if c[0] == "upload_save"]
        assert any(c[2].get("save_id") == 50 for c in upload_calls)
        # tracked_save_id still 50
        file_state = svc._save_sync_state.saves["42"].files["pokemon.srm"]
        assert file_state.tracked_save_id == 50
