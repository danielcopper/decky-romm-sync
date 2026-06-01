"""Tests for StatusService — save-status DTO building and read-only status checks."""

import asyncio
import threading

import pytest

from domain.rom_save_state import RomSaveState
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _file_md5,
    _get_save_state,
    _install_rom,
    _seed_save_state,
    _seed_save_state_dict,
    _server_save,
    _server_save_with_syncs,
    _set_device_id,
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
        _set_device_id(svc, "server-dev-1")

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
        _seed_save_state(svc, 42, RomSaveState(active_slot="other"))

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

        _seed_save_state_dict(
            svc,
            42,
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "0" * 32,
                        "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                    }
                }
            },
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
        _seed_save_state_dict(
            svc,
            42,
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": local_hash,
                        "last_sync_server_updated_at": ss_skip["updated_at"],
                    }
                }
            },
        )
        result_skip = svc._status._get_save_status_io(42, [ss_skip])
        assert result_skip["files"][0]["status"] == "synced"

        # ---------- Upload ----------
        # Reset state for next case: no server saves
        _seed_save_state(svc, 42, RomSaveState())
        result_upload = svc._status._get_save_status_io(42, [])
        assert result_upload["files"][0]["status"] == "upload"

        # ---------- Download ----------
        # Server moved past us, local matches baseline → Download
        ss_dl = _server_save_with_syncs(
            device_syncs=[{"device_id": "device-1", "is_current": False}],
        )
        fake.saves[100] = ss_dl
        _seed_save_state_dict(
            svc,
            42,
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": local_hash,
                        "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                    }
                }
            },
        )
        result_dl = svc._status._get_save_status_io(42, [ss_dl])
        assert result_dl["files"][0]["status"] == "download"

        # ---------- Conflict ----------
        _seed_save_state_dict(
            svc,
            42,
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "0" * 32,
                        "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                    }
                }
            },
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

        _seed_save_state(svc, 42, RomSaveState())

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

        _seed_save_state(svc, 42, RomSaveState())

        result = svc._status._get_save_status_io(42, [])

        assert result["files"] == []
        assert result["conflicts"] == []


class TestSaveSyncDisplayEnrichment:
    """get_save_status ships a pre-computed save_sync_display alongside files/conflicts."""

    def test_empty_slot_display_no_saves(self, tmp_path):
        svc, _ = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _seed_save_state(svc, 42, RomSaveState())

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
        _seed_save_state_dict(
            svc,
            42,
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": local_hash,
                        "last_sync_server_updated_at": ss["updated_at"],
                    }
                },
                "last_sync_check_at": "2026-04-01T08:00:00+00:00",
            },
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
        _seed_save_state_dict(
            svc,
            42,
            {
                "files": {
                    "pokemon.srm": {
                        "tracked_save_id": 100,
                        "last_sync_hash": "0" * 32,
                        "last_sync_server_updated_at": "2025-01-01T00:00:00Z",
                    }
                }
            },
        )

        result = svc._status._get_save_status_io(42, [ss])

        assert result["save_sync_display"]["status"] == "conflict"
        assert result["save_sync_display"]["label"] == "Conflict"
        assert result["save_sync_display"]["last_sync_check_at"] is None


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


def _seed_baseline_adopt_scenario(svc, fake, tmp_path) -> str:
    """Seed the state that drives ``get_save_status`` to adopt + persist a baseline.

    A local file is present, the server save reports our device as
    ``is_current=True``, and the tracked entry has **no** ``last_sync_hash``
    baseline yet — so the matrix returns ``Skip(adopt_baseline=True)`` and the
    status RMW records the local hash as the new baseline (an observable
    ``rom_save_states.save``). Returns the local file's md5.
    """
    _enable_sync_with_device(svc)
    _install_rom(svc, tmp_path)
    save_path = _create_save(tmp_path, content=b"matches baseline")
    local_hash = _file_md5(str(save_path))

    ss = _server_save_with_syncs(device_syncs=[{"device_id": "device-1", "is_current": True}])
    fake.saves[100] = ss
    # Realistic (hashful) tracked entry, but deliberately no last_sync_hash
    # baseline — that absence is what triggers the adopt-baseline write.
    _seed_save_state_dict(
        svc,
        42,
        {
            "files": {
                "pokemon.srm": {
                    "tracked_save_id": 100,
                    "last_sync_server_updated_at": ss["updated_at"],
                    "last_sync_server_save_id": 100,
                    "last_sync_local_size": 1024,
                }
            }
        },
    )
    return local_hash


def _adopted_baseline(svc) -> str | None:
    """Read back the persisted baseline hash for pokemon.srm, or ``None``."""
    state = _get_save_state(svc, 42)
    if state is None or "pokemon.srm" not in state.files:
        return None
    return state.files["pokemon.srm"].last_sync_hash


async def _drain_until(predicate, *, attempts: int = 200, step: float = 0.005) -> bool:
    """Drive the loop + thread-pool executor until *predicate* holds (or attempts run out).

    ``run_in_executor`` resolves its result future via a cross-thread
    ``call_soon_threadsafe`` callback, so a bare ``asyncio.sleep(0)`` does not
    reliably drain an in-flight executor round-trip. A short real-time sleep
    per iteration lets the worker thread finish and its callback land. Returns
    whether *predicate* became true within the budget.
    """
    for _ in range(attempts):
        if predicate():
            return True
        await asyncio.sleep(step)
    return predicate()


class TestGetSaveStatusRomLockSerialization:
    """The status RMW (baseline adopt) must serialize under ``rom_lock``.

    ``get_save_status`` does a get→mutate→save of ``rom_save_states``. Held
    under ``SyncEngine.rom_lock(rom_id)``, it cannot interleave with a
    concurrent ``do_sync_rom_saves`` and clobber that sync's write (#871).
    """

    @pytest.mark.asyncio
    async def test_status_rmw_blocks_while_rom_lock_held(self, tmp_path):
        """While the per-ROM lock is held, get_save_status must not enter or persist its RMW.

        ``_get_save_status_io`` is the executor body that holds the entire
        read-modify-write. Spying on it gives a cross-thread signal for *when*
        the critical section starts. With the lock held by the test, the task
        must park on ``rom_lock`` *before* that body runs — so the spy must not
        fire and the baseline must stay unadopted until the lock is released.
        """
        svc, fake = make_service(tmp_path)
        local_hash = _seed_baseline_adopt_scenario(svc, fake, tmp_path)
        assert _adopted_baseline(svc) is None

        status_svc = svc._status
        entered_rmw = threading.Event()
        original_io = status_svc._get_save_status_io

        def spy_io(*args, **kwargs):
            entered_rmw.set()
            return original_io(*args, **kwargs)

        status_svc._get_save_status_io = spy_io  # type: ignore[method-assign]

        engine = svc._sync_engine
        async with engine.rom_lock(42):
            task = asyncio.create_task(svc.get_save_status(42))
            # Drain the loop + executor so the lock-free network-fetch round
            # trips complete and the task genuinely reaches the rom_lock await.
            # If the lock did NOT guard the RMW, the executor body (spy) would
            # fire here. Give it a generous window to *try*.
            await _drain_until(entered_rmw.is_set)

            assert not entered_rmw.is_set(), "RMW executor body ran while rom_lock was held"
            assert not task.done(), "get_save_status completed while rom_lock was held"
            # Non-vacuous: the persisted baseline is still unadopted.
            assert _adopted_baseline(svc) is None

        # Lock released → the task acquires it, runs the RMW body, and persists.
        result = await asyncio.wait_for(task, timeout=5)

        assert entered_rmw.is_set()
        assert result["files"][0]["status"] == "synced"
        # Observe the actual persisted state change, not just that a call happened.
        assert _adopted_baseline(svc) == local_hash

    @pytest.mark.asyncio
    async def test_status_rmw_runs_when_lock_free(self, tmp_path):
        """Control: with no contender holding the lock, the RMW persists immediately."""
        svc, fake = make_service(tmp_path)
        local_hash = _seed_baseline_adopt_scenario(svc, fake, tmp_path)

        assert _adopted_baseline(svc) is None
        result = await svc.get_save_status(42)

        assert result["files"][0]["status"] == "synced"
        assert _adopted_baseline(svc) == local_hash

    @pytest.mark.asyncio
    async def test_status_does_not_block_on_other_rom_lock(self, tmp_path):
        """Holding rom_lock for a different rom_id must not stall this ROM's status RMW.

        Proves the lock is per-ROM, not global: rom 42's status RMW completes
        and persists while the test holds ``rom_lock(999)``.
        """
        svc, fake = make_service(tmp_path)
        local_hash = _seed_baseline_adopt_scenario(svc, fake, tmp_path)

        engine = svc._sync_engine
        async with engine.rom_lock(999):
            result = await asyncio.wait_for(svc.get_save_status(42), timeout=5)

        assert result["files"][0]["status"] == "synced"
        assert _adopted_baseline(svc) == local_hash
