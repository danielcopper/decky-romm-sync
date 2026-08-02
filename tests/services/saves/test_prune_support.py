"""Tests for PruneSaveSupport — exact-path save ownership, locking, and quarantine."""

from pathlib import Path

import pytest

from domain.rom_save_sync_state import FileSyncState, RomSaveSyncState
from tests.services.saves._helpers import (
    _create_save,
    _install_rom,
    _seed_rom,
    make_service,
)


class TestLockPruneRoms:
    async def test_locks_every_requested_rom_in_ascending_order(self, tmp_path):
        svc, _ = make_service(tmp_path)
        support = svc.prune_support
        acquired: list[int] = []
        original = svc._sync_engine.rom_lock

        def recording_lock(rom_id: int):
            acquired.append(rom_id)
            return original(rom_id)

        svc._sync_engine.rom_lock = recording_lock

        async with support.lock_prune_roms([9, 3, 3, 7]):
            assert acquired == [3, 7, 9]

    async def test_empty_id_list_locks_nothing_and_still_yields(self, tmp_path):
        svc, _ = make_service(tmp_path)
        entered = False

        async with svc.prune_support.lock_prune_roms([]):
            entered = True

        assert entered is True

    async def test_releases_every_lock_when_the_body_raises(self, tmp_path):
        svc, _ = make_service(tmp_path)
        support = svc.prune_support

        locked = support.lock_prune_roms([4, 5])
        with pytest.raises(RuntimeError, match="body failed"):
            async with locked:
                raise RuntimeError("body failed")

        # A second acquisition can only complete if the first released.
        async with support.lock_prune_roms([4, 5]):
            pass


class TestPruneSaveInventory:
    def test_persisted_known_filename_and_matching_history_are_recovery_artifacts(self, tmp_path):
        svc, _ = make_service(tmp_path)
        support = svc.prune_support
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="renamed.gba")
        saves_dir = tmp_path / "saves" / "gba"
        saves_dir.mkdir(parents=True)
        historical = saves_dir / "old-name.srm"
        historical.write_bytes(b"save")
        backup_dir = saves_dir / ".romm-backup"
        backup_dir.mkdir()
        matching_backup = backup_dir / "old-name_20260101_120000.srm"
        matching_backup.write_bytes(b"history")
        (backup_dir / "other_20260101_120000.srm").write_bytes(b"other")
        state = RomSaveSyncState(system="gba", files={"old-name.srm": FileSyncState(last_sync_hash="known")})
        with svc._uow_factory() as uow:
            uow.rom_save_sync_states.save(42, state)

        inventory = support.inventory_prune_saves([42])

        artifact_paths = {item["source_path"] for item in inventory["artifacts"]}
        expected_current = {str(saves_dir / f"renamed.{extension}") for extension in ("srm", "rtc", "sav")}
        assert artifact_paths == {str(historical), str(matching_backup), *expected_current}
        assert {item["path"] for item in inventory["exclusive"]} == {str(historical), *expected_current}

    def test_shared_save_expands_lock_owners_and_is_never_quarantined(self, tmp_path):
        svc, _ = make_service(tmp_path)
        support = svc.prune_support
        _install_rom(svc, tmp_path, rom_id=1, system="gba", file_name="shared.gba")
        _install_rom(svc, tmp_path, rom_id=2, system="gba", file_name="shared.gba")
        shared = _create_save(tmp_path, system="gba", rom_name="shared", content=b"shared")

        inventory = support.inventory_prune_saves([1])

        assert inventory["exclusive"] == []
        assert inventory["shared"] == [str(shared)]
        assert inventory["lock_rom_ids"] == [1, 2]

    def test_uninstalled_live_replacement_keeps_its_shared_save_in_place(self, tmp_path):
        """The canonical vanished/replacement pair: purge row installed, live row not.

        Both rows project onto the same save path, so the locked recovery
        contract copies the file into the bundle and leaves it where the
        replacement will read it — quarantining it would strand the live
        version's progress.
        """
        svc, _ = make_service(tmp_path)
        support = svc.prune_support
        _install_rom(svc, tmp_path, rom_id=4375, system="gba", file_name="game.gba")
        _seed_rom(svc, 25135, platform_slug="gba", fs_name="game.gba")
        shared = _create_save(tmp_path, system="gba", rom_name="game", content=b"live progress")

        inventory = support.inventory_prune_saves([4375])

        assert inventory["exclusive"] == []
        assert inventory["shared"] == [str(shared)]
        assert {item["source_path"] for item in inventory["artifacts"]} == {str(shared)}
        assert inventory["source_claims"] == {}
        assert inventory["lock_rom_ids"] == [4375, 25135]

        result = support.quarantine_prune_saves(inventory["exclusive"], inventory["source_claims"])

        assert result == {"success": True, "moved": [], "ambiguous": False}
        assert shared.read_bytes() == b"live progress"

    def test_uninstalled_row_with_a_different_content_name_stays_exclusive(self, tmp_path):
        """No over-correction: an unrelated uninstalled row must not shield the save.

        It projects onto a different stem, so the purge row still owns its saves
        alone and they leave through the ``.romm-backup`` quarantine funnel.
        """
        svc, _ = make_service(tmp_path)
        support = svc.prune_support
        _install_rom(svc, tmp_path, rom_id=4375, system="gba", file_name="game.gba")
        _seed_rom(svc, 25135, platform_slug="gba", fs_name="other-game.gba")
        owned = _create_save(tmp_path, system="gba", rom_name="game", content=b"only mine")

        inventory = support.inventory_prune_saves([4375])

        expected_paths = {str(tmp_path / "saves" / "gba" / f"game.{extension}") for extension in ("srm", "rtc", "sav")}
        assert {item["path"] for item in inventory["exclusive"]} == expected_paths
        assert inventory["shared"] == []
        assert inventory["lock_rom_ids"] == [4375]

        result = support.quarantine_prune_saves(inventory["exclusive"], inventory["source_claims"])

        assert result["success"] is True
        assert result["moved"] == [str(owned)]
        assert owned.exists() is False
        backups = list((tmp_path / "saves" / "gba" / ".romm-backup").glob("game_*.srm"))
        assert [path.read_bytes() for path in backups] == [b"only mine"]

    def test_missing_exclusive_save_is_claimed_absent_and_late_creation_is_retained(self, tmp_path):
        svc, _ = make_service(tmp_path)
        support = svc.prune_support
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="late.gba")
        expected = str(tmp_path / "saves" / "gba" / "late.srm")

        inventory = support.inventory_prune_saves([42])

        expected_paths = {str(tmp_path / "saves" / "gba" / f"late.{extension}") for extension in ("srm", "rtc", "sav")}
        assert {item["path"] for item in inventory["exclusive"]} == expected_paths
        assert {item["source_path"] for item in inventory["artifacts"]} == expected_paths
        assert set(inventory["source_claims"]) == expected_paths
        assert inventory["source_claims"][expected]["source_identity"]["exists"] is False

        Path(expected).parent.mkdir(parents=True, exist_ok=True)
        Path(expected).write_bytes(b"created by emulator")
        result = support.quarantine_prune_saves(inventory["exclusive"], inventory["source_claims"])

        assert result["success"] is False
        assert "appeared after sealing" in result["message"]
        assert Path(expected).read_bytes() == b"created by emulator"

    def test_expected_absence_is_rechecked_after_quarantine_before_cascade(self, tmp_path):
        svc, _ = make_service(tmp_path)
        support = svc.prune_support
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="late.gba")
        expected = Path(tmp_path / "saves" / "gba" / "late.srm")
        expected.parent.mkdir(parents=True)
        inventory = support.inventory_prune_saves([42])

        result = support.quarantine_prune_saves(inventory["exclusive"], inventory["source_claims"])
        assert result["success"] is True

        expected.parent.mkdir(parents=True, exist_ok=True)
        expected.write_bytes(b"created after absence was consumed")

        assert support.validate_prune_absences(inventory["source_claims"]) is False
        assert expected.read_bytes() == b"created after absence was consumed"

    def test_quarantined_present_save_is_rechecked_for_recreation_before_cascade(self, tmp_path):
        svc, _ = make_service(tmp_path)
        support = svc.prune_support
        _install_rom(svc, tmp_path, rom_id=42, system="gba", file_name="present.gba")
        expected = _create_save(tmp_path, system="gba", rom_name="present", content=b"sealed-current")
        inventory = support.inventory_prune_saves([42])
        assert inventory["source_claims"][str(expected)]["source_identity"]["exists"] is True

        result = support.quarantine_prune_saves(inventory["exclusive"], inventory["source_claims"])
        assert result["success"] is True
        expected.write_bytes(b"emulator-recreated")

        assert support.validate_prune_absences(inventory["source_claims"]) is False
        assert expected.read_bytes() == b"emulator-recreated"
