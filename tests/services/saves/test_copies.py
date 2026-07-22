"""Tests for SaveCopyService — the per-save copy-to-slot flow.

The copy takes one server save (a named slot or the legacy no-slot bucket)
and copies its content into a target slot, which becomes the ROM's active
slot with the copied save as its current save. The source save is never
deleted. A distinctive target name (never the default "autosave" fallback)
is used throughout so the tests prove the target value actually flows.
"""

from typing import Any

import pytest

from domain.save_layout import ContentDir
from tests.services.saves._helpers import (
    _create_save,
    _enable_sync_with_device,
    _file_md5,
    _install_rom,
    _require_save_state,
    _seed_save_state_dict,
    _server_save,
    _server_save_with_syncs,
    make_service,
)

# Distinctive target slot — never the "autosave" default, so a passing test
# proves the value flowed through rather than a fallback landing on the default.
TARGET = "promoted"


def _setup_configured(
    svc,
    tmp_path,
    *,
    active_slot: str = "autosave",
    tracked_id: int | None = 100,
    last_sync_hash: str | None = None,
    system: str = "gba",
) -> None:
    """Seed an installed, sync-enabled, CONFIRMED-slot ROM (rom 42).

    Mirrors the rollback suite's ``_setup_state`` but sets ``slot_confirmed`` so
    the copy flow's ``not_configured`` guard passes. ``last_sync_hash=None`` seeds
    no per-file baseline (the matrix falls back to its in-memory default).
    """
    _install_rom(svc, tmp_path, system=system)
    _enable_sync_with_device(svc)
    files = (
        {"pokemon.srm": {"tracked_save_id": tracked_id, "last_sync_hash": last_sync_hash}}
        if last_sync_hash is not None
        else {}
    )
    _seed_save_state_dict(
        svc,
        42,
        {
            "system": system,
            "active_slot": active_slot,
            "slot_confirmed": True,
            "files": files,
        },
    )


def _tracked_save(save_id: int, *, slot: str, updated_at: str = "2026-03-10T10:00:00Z") -> dict[str, Any]:
    """A slot head with our device flagged ``is_current`` — a clean matrix pre-flight."""
    return _server_save_with_syncs(
        save_id=save_id,
        slot=slot,
        updated_at=updated_at,
        device_syncs=[{"device_id": "device-1", "is_current": True, "last_synced_at": updated_at}],
    )


class TestCopySaveToSlotHappyPaths:
    """The three happy paths from the spec: legacy→new named, default→autosave, inactive→active."""

    @pytest.mark.asyncio
    async def test_no_slot_save_into_new_named_slot(self, tmp_path):
        """A legacy (slot:null) save copied into a brand-new named slot.

        The original scope of the issue: promote a no-slot save into a named
        slot. The new slot becomes active; the legacy source is preserved.
        """
        svc, fake = make_service(tmp_path)
        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        _setup_configured(svc, tmp_path, active_slot="autosave", tracked_id=100, last_sync_hash=local_hash)
        # Current-slot head (clean pre-flight) + a legacy no-slot save to copy.
        fake.saves[100] = _tracked_save(100, slot="autosave")
        fake.saves[10] = _server_save(save_id=10, rom_id=42, slot=None, updated_at="2026-02-01T10:00:00Z")
        fake.set_server_save_content(10, b"legacy-save-bytes")

        result = await svc.copy_save_to_slot(42, 10, TARGET)

        assert result == {"status": "ok"}
        state = _require_save_state(svc, 42)
        assert state.active_slot == TARGET
        assert state.slot_confirmed is True
        # Source preserved (still a legacy no-slot save).
        assert 10 in fake.saves
        assert fake.saves[10].get("slot") is None
        # A new save landed in the target slot.
        assert [s for s in fake.saves.values() if s.get("slot") == TARGET]

    @pytest.mark.asyncio
    async def test_default_save_into_autosave(self, tmp_path):
        """A save in the old ``default`` slot copied onto ``autosave`` (#1529 manual path).

        ``default`` is the active slot; copying its save into ``autosave`` makes
        autosave active. Uses the real default target name here (the value under
        test IS "autosave"), distinct from the source "default".
        """
        svc, fake = make_service(tmp_path)
        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        _setup_configured(svc, tmp_path, active_slot="default", tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = _tracked_save(100, slot="default")

        result = await svc.copy_save_to_slot(42, 100, "autosave")

        assert result == {"status": "ok"}
        state = _require_save_state(svc, 42)
        assert state.active_slot == "autosave"
        assert state.slot_confirmed is True
        # Source in "default" preserved.
        assert fake.saves[100].get("slot") == "default"
        # A new save landed in autosave.
        assert [s for s in fake.saves.values() if s.get("slot") == "autosave"]

    @pytest.mark.asyncio
    async def test_inactive_slot_save_into_active_slot(self, tmp_path):
        """A save from an INACTIVE slot copied into the active slot.

        The active slot already holds a head we're synced to, so the target POST
        stacks cleanly (no 409). The inactive source save is preserved.
        """
        svc, fake = make_service(tmp_path)
        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        _setup_configured(svc, tmp_path, active_slot="autosave", tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = _tracked_save(100, slot="autosave")
        # We're current on the active head per the ledger, so a POST into autosave
        # does not 409 on it.
        fake.stage_device_sync(100, "device-1", "2026-03-10T10:00:00Z")
        # Inactive-slot source save.
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="backup", updated_at="2026-02-01T10:00:00Z")

        result = await svc.copy_save_to_slot(42, 50, "autosave")

        assert result == {"status": "ok"}
        state = _require_save_state(svc, 42)
        assert state.active_slot == "autosave"
        # Source in the inactive slot preserved.
        assert fake.saves[50].get("slot") == "backup"
        # A new save was created (id from the fake's 1000+ range), distinct from
        # both the source and the pre-existing head.
        autosave_ids = {s["id"] for s in fake.saves.values() if s.get("slot") == "autosave"}
        assert autosave_ids - {100} != set()  # at least one NEW save id in autosave

    @pytest.mark.asyncio
    async def test_source_preserved_and_target_becomes_active(self, tmp_path):
        """Dedicated non-vacuous check: the source save survives AND the target is active."""
        svc, fake = make_service(tmp_path)
        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        _setup_configured(svc, tmp_path, active_slot="autosave", tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = _tracked_save(100, slot="autosave")
        fake.saves[10] = _server_save(save_id=10, rom_id=42, slot=None, updated_at="2026-02-01T10:00:00Z")

        before_ids = set(fake.saves)
        result = await svc.copy_save_to_slot(42, 10, TARGET)

        assert result == {"status": "ok"}
        # The source id is still present (a copy, never a move/delete).
        assert 10 in fake.saves
        # The target is now the ROM's active slot.
        assert _require_save_state(svc, 42).active_slot == TARGET
        # A brand-new save id appeared for the copy (the source was not mutated in place).
        assert set(fake.saves) - before_ids


class TestCopySaveToSlotRefusals:
    """Every refusal branch of the discriminated-status union."""

    @pytest.mark.asyncio
    async def test_invalid_slot_name_empty(self, tmp_path):
        svc, _fake = make_service(tmp_path)
        assert await svc.copy_save_to_slot(42, 10, "") == {"status": "invalid_slot_name"}

    @pytest.mark.asyncio
    async def test_invalid_slot_name_whitespace(self, tmp_path):
        svc, _fake = make_service(tmp_path)
        assert await svc.copy_save_to_slot(42, 10, "   ") == {"status": "invalid_slot_name"}

    @pytest.mark.asyncio
    async def test_not_configured_when_slot_unconfirmed(self, tmp_path):
        """An installed ROM whose slot is not confirmed is refused up front (#1529 lesson)."""
        svc, _fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _enable_sync_with_device(svc)
        # active_slot set but slot_confirmed defaults False → not configured.
        _seed_save_state_dict(svc, 42, {"system": "gba", "active_slot": "autosave"})

        assert await svc.copy_save_to_slot(42, 10, TARGET) == {"status": "not_configured"}

    @pytest.mark.asyncio
    async def test_not_configured_when_legacy_active_slot(self, tmp_path):
        """The legacy no-slot mode ("") is not a confirmed named slot — refused."""
        svc, _fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _enable_sync_with_device(svc)
        _seed_save_state_dict(svc, 42, {"system": "gba", "active_slot": "", "slot_confirmed": True})

        assert await svc.copy_save_to_slot(42, 10, TARGET) == {"status": "not_configured"}

    @pytest.mark.asyncio
    async def test_rom_not_installed(self, tmp_path):
        """A configured ROM that isn't installed locally → rom_not_installed."""
        svc, _fake = make_service(tmp_path)
        # Configured state but no rom_installs row.
        _seed_save_state_dict(svc, 42, {"system": "gba", "active_slot": "autosave", "slot_confirmed": True})

        result = await svc.copy_save_to_slot(42, 10, TARGET)
        assert result == {"status": "rom_not_installed"}

    @pytest.mark.asyncio
    async def test_multi_file_slot_unsupported(self, tmp_path):
        """A multi-file slot (e.g. Saturn .bkr/.bcr) refuses the copy (#908 guard)."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path, system="saturn", file_name="rally.cue")
        _enable_sync_with_device(svc)
        _create_save(tmp_path, system="saturn", rom_name="rally", ext=".bkr")
        _create_save(tmp_path, system="saturn", rom_name="rally", ext=".bcr")
        _seed_save_state_dict(svc, 42, {"system": "saturn", "active_slot": "autosave", "slot_confirmed": True})
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="backup", updated_at="2026-02-01T10:00:00Z")

        result = await svc.copy_save_to_slot(42, 50, TARGET)

        assert result == {"status": "unsupported"}
        # No destructive I/O ran — the guard fired before any list/download/upload.
        assert not any(c[0] in ("upload_save", "download_save_content", "list_saves") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_content_dir_unsupported(self, tmp_path):
        """RetroArch content-dir layout (#239) refuses the copy with the reason slug."""
        svc, fake = make_service(tmp_path, detect_sort_change=lambda: ContentDir())
        _install_rom(svc, tmp_path)
        _enable_sync_with_device(svc)
        _create_save(tmp_path)
        _seed_save_state_dict(svc, 42, {"system": "gba", "active_slot": "autosave", "slot_confirmed": True})
        fake.saves[50] = _server_save(save_id=50, rom_id=42, slot="backup", updated_at="2026-02-01T10:00:00Z")

        result = await svc.copy_save_to_slot(42, 50, TARGET)

        assert result == {"status": "unsupported", "reason": "savefiles_in_content_dir"}
        # Gate fired before any I/O.
        assert not any(c[0] in ("upload_save", "download_save_content", "list_saves") for c in fake.call_log)

    @pytest.mark.asyncio
    async def test_version_deleted_when_save_id_missing(self, tmp_path):
        """A clean pre-flight, but the chosen save id is absent from the server list."""
        svc, fake = make_service(tmp_path)
        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        _setup_configured(svc, tmp_path, active_slot="autosave", tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = _tracked_save(100, slot="autosave")

        result = await svc.copy_save_to_slot(42, 999, TARGET)

        assert result == {"status": "version_deleted"}

    @pytest.mark.asyncio
    async def test_server_unreachable_on_post_preflight_list(self, tmp_path):
        """A ``list_saves`` failure AFTER a clean pre-flight → server_unreachable."""
        svc, fake = make_service(tmp_path)
        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        _setup_configured(svc, tmp_path, active_slot="autosave", tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = _tracked_save(100, slot="autosave")

        original_list = fake.list_saves
        call_count = {"n": 0}

        def fail_second_list(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] >= 2:
                raise OSError("server unreachable after preflight")
            return original_list(*args, **kwargs)

        fake.list_saves = fail_second_list  # type: ignore[method-assign]
        try:
            result = await svc.copy_save_to_slot(42, 100, TARGET)
        finally:
            fake.list_saves = original_list  # type: ignore[method-assign]

        assert result["status"] == "server_unreachable"
        assert "unreachable" in result.get("message", "").lower()
        assert "error" not in result

    @pytest.mark.asyncio
    async def test_target_slot_busy_on_409(self, tmp_path):
        """The target slot has a newer foreign save this device hasn't synced → 409 → target_slot_busy."""
        svc, fake = make_service(tmp_path)
        _create_save(tmp_path)
        local_hash = _file_md5(str(tmp_path / "saves" / "gba" / "pokemon.srm"))
        _setup_configured(svc, tmp_path, active_slot="autosave", tracked_id=100, last_sync_hash=local_hash)
        fake.saves[100] = _tracked_save(100, slot="autosave")
        # A legacy source to copy.
        fake.saves[10] = _server_save(save_id=10, rom_id=42, slot=None, updated_at="2026-02-01T10:00:00Z")
        # The target slot already holds a foreign head we've never synced.
        fake.seed_foreign_save(42, save_id=77, slot=TARGET, uploaded_by="device-B")

        result = await svc.copy_save_to_slot(42, 10, TARGET)

        assert result["status"] == "target_slot_busy"
        assert result.get("message")
        # The source save is untouched.
        assert 10 in fake.saves

    @pytest.mark.asyncio
    async def test_conflict_blocked_on_dirty_current_slot(self, tmp_path):
        """A real conflict on the CURRENT slot blocks the copy before any copy I/O."""
        svc, fake = make_service(tmp_path)
        _enable_sync_with_device(svc)
        _install_rom(svc, tmp_path)
        _seed_save_state_dict(
            svc,
            42,
            {
                "system": "gba",
                "active_slot": "autosave",
                "slot_confirmed": True,
                "files": {"pokemon.srm": {"tracked_save_id": 100, "last_sync_hash": "aabbcc001122334455667788"}},
            },
        )
        # Local diverged from the baseline, and the server head moved past us
        # (is_current False) → the matrix returns Conflict.
        _create_save(tmp_path, content=b"\xff" * 1024)
        fake.saves[100] = _server_save_with_syncs(
            save_id=100,
            slot="autosave",
            updated_at="2026-03-15T10:00:00Z",
            device_syncs=[{"device_id": "device-1", "is_current": False, "last_synced_at": "2026-03-10T10:00:00Z"}],
        )
        fake.saves[10] = _server_save(save_id=10, rom_id=42, slot=None, updated_at="2026-02-01T10:00:00Z")

        result = await svc.copy_save_to_slot(42, 10, TARGET)

        assert result["status"] == "conflict_blocked"
        assert len(result["conflicts"]) == 1
        # No copy download of the source ran, and the target slot never received a save.
        download_ids = [c[1][0] for c in fake.call_log if c[0] == "download_save_content"]
        assert 10 not in download_ids
        assert not [s for s in fake.saves.values() if s.get("slot") == TARGET]

    @pytest.mark.asyncio
    async def test_preflight_failed_on_server_error(self, tmp_path):
        """A non-conflict error during the pre-flight aborts with preflight_failed."""
        svc, fake = make_service(tmp_path)
        _install_rom(svc, tmp_path)
        _enable_sync_with_device(svc)
        _seed_save_state_dict(svc, 42, {"system": "gba", "active_slot": "autosave", "slot_confirmed": True})
        fake.fail_on_next(Exception("network error"))

        result = await svc.copy_save_to_slot(42, 10, TARGET)

        assert result["status"] == "preflight_failed"
        assert any("network" in err.lower() for err in result.get("errors", []))
        # No copy ran — the target slot got no save.
        assert not [s for s in fake.saves.values() if s.get("slot") == TARGET]
