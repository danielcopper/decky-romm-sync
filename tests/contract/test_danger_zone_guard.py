"""Contract tests for the danger-zone sync guard (#1390).

The three bulk-removal callables (``remove_all_shortcuts``,
``remove_platform_shortcuts``, ``uninstall_all_roms``) refuse with the
canonical failure shape ``{success: False, reason: "sync_active", message}``
while a library-sync run is in flight (RUNNING or CANCELLING) — a bulk
removal racing a running sync would delete shortcuts the apply is writing
and corrupt the registry mid-run. At IDLE (which paused and completed runs
reset the live state to) each callable answers with its normal shape.

Driven through the real callables over the real wired plugin, frontend-shaped
(positional args), asserting the response shape on both sides of the gate.
"""

from __future__ import annotations

import pytest

from domain.sync_state import SyncState

_IN_FLIGHT_STATES = [SyncState.RUNNING, SyncState.CANCELLING]


def _assert_sync_active_refusal(result):
    """Pin the gate's canonical failure shape: exactly success/reason/message."""
    assert set(result) == {"success", "reason", "message"}
    assert result["success"] is False
    assert result["reason"] == "sync_active"
    assert isinstance(result["message"], str)
    assert result["message"]


@pytest.mark.parametrize("state", _IN_FLIGHT_STATES)
async def test_remove_all_shortcuts_refused_while_in_flight(harness, state):
    harness.plugin._sync_service._box.sync_state = state
    result = await harness.plugin.remove_all_shortcuts()
    _assert_sync_active_refusal(result)


@pytest.mark.parametrize("state", _IN_FLIGHT_STATES)
async def test_remove_platform_shortcuts_refused_while_in_flight(harness, state):
    harness.plugin._sync_service._box.sync_state = state
    result = await harness.plugin.remove_platform_shortcuts("n64")
    _assert_sync_active_refusal(result)


@pytest.mark.parametrize("state", _IN_FLIGHT_STATES)
async def test_uninstall_all_roms_refused_while_in_flight(harness, state):
    harness.plugin._sync_service._box.sync_state = state
    result = await harness.plugin.uninstall_all_roms()
    _assert_sync_active_refusal(result)


async def test_remove_all_shortcuts_normal_shape_at_idle(harness):
    """IDLE: the callable answers its normal success shape (empty registry)."""
    result = await harness.plugin.remove_all_shortcuts()
    assert result["success"] is True
    assert result["app_ids"] == []
    assert result["rom_ids"] == []


async def test_remove_platform_shortcuts_normal_shape_at_idle(harness):
    """IDLE: the callable answers its normal shape (name degrades to the slug)."""
    result = await harness.plugin.remove_platform_shortcuts("n64")
    assert result["success"] is True
    assert result["app_ids"] == []
    assert result["rom_ids"] == []
    assert result["platform_name"] == "n64"


async def test_uninstall_all_roms_partial_success_shape_at_idle(harness):
    """IDLE: the callable answers its partial-success shape (nothing installed)."""
    result = await harness.plugin.uninstall_all_roms()
    assert result == {"success": True, "removed_count": 0, "errors": [], "app_ids": []}
