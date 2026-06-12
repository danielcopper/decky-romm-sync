"""Contract tests for the save-sync read-surface callables.

Driven frontend-shaped per ``src/api/backend.ts`` (positional args, the TS
arg types). The failure-shape assertions are the ones that guard the
#1009-class bug (frontend ignoring ``success: false``) and the #1004-class
partial-success carve-outs: proving the backend returns the canonical /
discriminated / additive-flag shapes makes the contract explicit.

Failure injection note: the save read paths run their RomM calls through
``http_adapter.with_retry``, which the harness neutralises to a single
attempt (no backoff sleep). A ``RommConnectionError`` (a real
server-unreachable transport error) is therefore fast to inject.

Explicitly OUT of Phase 1 (NOT tested here, named as deferred):

- ``confirm_slot_choice`` and ``switch_slot`` — their fixed contract is not
  decided until #1004 / #1005; the contract tests land with that fix.
- ``delete_slot`` / ``resolve_sync_conflict`` / ``sync_rom_saves`` and the
  other mutation flows — mutation + event contracts (#1017) land with their
  own fixes; this tier covers the read surface only.
"""

from __future__ import annotations

from lib.errors import RommConnectionError
from lib.list_result import ErrorCode

from ._seed import enable_save_sync, seed_confirmed_slot, seed_install, seed_rom, seed_server_save

# ── get_save_status ──────────────────────────────────────────────────────


async def test_get_save_status_full_shape_and_partial_flag(harness):
    """Happy path: full payload, with the additive server_query_failed flag present and a bool."""
    enable_save_sync(harness)
    result = await harness.plugin.get_save_status(42)
    expected_keys = {
        "rom_id",
        "files",
        "playtime",
        "device_id",
        "last_sync_check_at",
        "conflicts",
        "save_sort_changed",
        "savefiles_in_content_dir",
        "save_sync_display",
        "server_query_failed",
        "multi_file",
        "component_files",
        "rollback_supported",
    }
    assert set(result.keys()) == expected_keys
    assert result["rom_id"] == 42
    assert isinstance(result["files"], list)
    assert isinstance(result["conflicts"], list)
    # Partial-success carve-out: the additive flag is present and a bool.
    assert isinstance(result["server_query_failed"], bool)
    assert result["server_query_failed"] is False


async def test_get_save_status_server_failure_keeps_full_payload(harness):
    """Server unreachable: server_query_failed True, full payload still returned (partial-success)."""
    enable_save_sync(harness)
    harness.romm.list_saves_side_effect = RommConnectionError("offline")
    result = await harness.plugin.get_save_status(42)
    # The carve-out: a failure flag rides alongside the full payload, not a
    # bare {success: False}. The frontend still renders local state.
    assert result["server_query_failed"] is True
    assert result["rom_id"] == 42
    assert "files" in result
    assert "save_sync_display" in result


# ── get_save_slots ───────────────────────────────────────────────────────


async def test_get_save_slots_happy_shape(harness):
    enable_save_sync(harness)
    # get_save_slots persists the merged slot listing → its rom_save_states write
    # needs the roms FK parent (a synced ROM always has one in production).
    seed_rom(harness, 42)
    seed_server_save(harness, save_id=500, rom_id=42, slot="main")
    result = await harness.plugin.get_save_slots(42)
    assert result["success"] is True
    assert isinstance(result["slots"], list)
    assert isinstance(result["active_slot"], str)
    if result["slots"]:
        slot = result["slots"][0]
        assert set(slot.keys()) == {"slot", "source", "count", "latest_updated_at"}


async def test_get_save_slots_sync_disabled_failure_shape(harness):
    """Sync disabled → failure shape WITH fallback fields the frontend renders."""
    # save_sync_enabled defaults to False — do not enable it.
    result = await harness.plugin.get_save_slots(42)
    assert result["success"] is False
    assert result["reason"] == "sync_disabled"
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["slots"] == []
    assert result["active_slot"] == "default"


async def test_get_save_slots_server_failure_shape(harness):
    """Server unreachable → canonical SERVER_UNREACHABLE failure with fallbacks."""
    enable_save_sync(harness)
    harness.romm.get_save_summary_side_effect = RommConnectionError("offline")
    result = await harness.plugin.get_save_slots(42)
    assert result["success"] is False
    assert result["reason"] == ErrorCode.SERVER_UNREACHABLE
    assert isinstance(result["message"], str)
    assert result["message"]
    assert result["slots"] == []
    assert "active_slot" in result


# ── get_slot_saves ───────────────────────────────────────────────────────


async def test_get_slot_saves_happy_shape(harness):
    enable_save_sync(harness)
    seed_server_save(harness, save_id=600, rom_id=42, slot="main", file_name="game.srm")
    result = await harness.plugin.get_slot_saves(42, "main")
    assert result["success"] is True
    assert result["slot"] == "main"
    assert isinstance(result["saves"], list)
    assert len(result["saves"]) == 1
    save = result["saves"][0]
    assert set(save.keys()) == {"filename", "id", "size", "updated_at", "emulator"}
    assert save["filename"] == "game.srm"
    assert save["id"] == 600


async def test_get_slot_saves_server_failure_shape(harness):
    enable_save_sync(harness)
    harness.romm.list_saves_side_effect = RommConnectionError("offline")
    result = await harness.plugin.get_slot_saves(42, "main")
    assert result["success"] is False
    assert result["reason"] == ErrorCode.SERVER_UNREACHABLE
    assert isinstance(result["message"], str)
    assert result["slot"] == "main"
    assert result["saves"] == []


# ── get_slot_delete_info ─────────────────────────────────────────────────


async def test_get_slot_delete_info_happy_shape(harness):
    """Installed + tracked slot → full delete-info shape."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", platform_slug="gba")
    seed_confirmed_slot(harness, 42, slot="main", source="server")
    seed_server_save(harness, save_id=700, rom_id=42, slot="main")
    result = await harness.plugin.get_slot_delete_info(42, "main")
    assert result["success"] is True
    assert set(result.keys()) == {
        "success",
        "slot",
        "source",
        "server_save_count",
        "server_save_ids",
        "local_file_count",
        "local_filenames",
        "is_active",
    }
    assert result["slot"] == "main"
    assert result["is_active"] is True
    assert isinstance(result["server_save_ids"], list)


async def test_get_slot_delete_info_not_installed_failure_shape(harness):
    """Documented guard: sync on but not installed → canonical {success: False, reason, message}."""
    enable_save_sync(harness)
    result = await harness.plugin.get_slot_delete_info(42, "main")
    assert result == {"success": False, "reason": "not_installed", "message": "ROM is not installed"}


async def test_get_slot_delete_info_server_failure_shape(harness):
    """Server unreachable while inspecting a server slot → SERVER_UNREACHABLE."""
    enable_save_sync(harness)
    seed_install(harness, 42, system="gba", platform_slug="gba")
    seed_confirmed_slot(harness, 42, slot="main", source="server")
    harness.romm.list_saves_side_effect = RommConnectionError("offline")
    result = await harness.plugin.get_slot_delete_info(42, "main")
    assert result["success"] is False
    assert result["reason"] == ErrorCode.SERVER_UNREACHABLE
    assert isinstance(result["message"], str)
    assert result["message"]


# ── is_save_tracking_configured ──────────────────────────────────────────


async def test_is_save_tracking_configured_unconfigured_shape(harness):
    result = await harness.plugin.is_save_tracking_configured(42)
    assert result == {"configured": False, "active_slot": None}


async def test_is_save_tracking_configured_configured_shape(harness):
    seed_confirmed_slot(harness, 42, slot="main")
    result = await harness.plugin.is_save_tracking_configured(42)
    assert result["configured"] is True
    assert result["active_slot"] == "main"


# ── get_save_setup_info ──────────────────────────────────────────────────


async def test_get_save_setup_info_happy_shape(harness):
    enable_save_sync(harness)
    seed_server_save(harness, save_id=800, rom_id=42, slot="main")
    result = await harness.plugin.get_save_setup_info(42)
    expected_keys = {
        "has_local_saves",
        "local_files",
        "server_slots",
        "default_slot",
        "slot_confirmed",
        "active_slot",
        "recommended_action",
        "server_query_failed",
    }
    assert set(result.keys()) == expected_keys
    assert result["server_query_failed"] is False
    # Server answered → recommendation is authoritative, never the unreachable slug.
    assert result["recommended_action"] in {"auto_confirm_default", "show_wizard"}
    assert isinstance(result["server_slots"], list)


async def test_get_save_setup_info_server_failure_carve_out(harness):
    """Server unreachable → recommended_action carve-out + additive failure flag."""
    enable_save_sync(harness)
    harness.romm.list_saves_side_effect = RommConnectionError("offline")
    result = await harness.plugin.get_save_setup_info(42)
    # Partial-success carve-out: surface a distinct recommendation instead of
    # auto-confirming a default that could clobber real server saves.
    assert result["recommended_action"] == "server_unreachable"
    assert result["server_query_failed"] is True
    assert result["server_slots"] == []


# ── get_save_sync_settings ───────────────────────────────────────────────


async def test_get_save_sync_settings_shape(harness):
    result = await harness.plugin.get_save_sync_settings()
    assert set(result.keys()) == {
        "save_sync_enabled",
        "sync_before_launch",
        "sync_after_exit",
        "default_slot",
        "autocleanup_limit",
    }
    assert isinstance(result["save_sync_enabled"], bool)
    assert isinstance(result["sync_before_launch"], bool)
    assert isinstance(result["sync_after_exit"], bool)
    assert isinstance(result["autocleanup_limit"], int)


# ── saves_list_file_versions (discriminated-status union) ─────────────────


async def test_saves_list_file_versions_ok_shape(harness):
    """status == 'ok' discriminant + versions list."""
    enable_save_sync(harness)
    seed_server_save(harness, save_id=900, rom_id=42, slot="main", file_name="game.srm")
    result = await harness.plugin.saves_list_file_versions(42, "main", "game.srm")
    assert result["status"] == "ok"
    assert isinstance(result["versions"], list)
    if result["versions"]:
        v = result["versions"][0]
        assert set(v.keys()) == {
            "id",
            "file_name",
            "emulator",
            "updated_at",
            "file_size_bytes",
            "device_syncs",
            "uploaded_by_us",
        }


async def test_saves_list_file_versions_server_unreachable_shape(harness):
    """status == 'server_unreachable' carries message: str (NOT error)."""
    enable_save_sync(harness)
    harness.romm.list_saves_side_effect = RommConnectionError("offline")
    result = await harness.plugin.saves_list_file_versions(42, "main", "game.srm")
    assert result["status"] == "server_unreachable"
    assert isinstance(result["message"], str)
    assert result["message"]
    # The discriminated-status union uses `message`, never the legacy `error`.
    assert "error" not in result
