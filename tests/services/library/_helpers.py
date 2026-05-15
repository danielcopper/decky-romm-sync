"""Shared helpers for the LibraryService sub-service test files.

Mock-loop factories for the executor pattern (LibraryService runs
sync RomM calls in an executor) and small ROM/registry/page builders
used across :class:`TestFetchCollectionRoms`,
:class:`TestCollectionSyncEdgeCases`, and the facade-integration
collection tests.
"""

from unittest.mock import AsyncMock, MagicMock


def _make_loop_with_executor(*return_values):
    """Return a mock loop whose run_in_executor returns values in sequence.

    Each call to run_in_executor returns the next value from return_values.
    If only one value is given it is returned for every call.
    """
    mock_loop = MagicMock()
    if len(return_values) == 1:
        mock_loop.run_in_executor = AsyncMock(return_value=return_values[0])
    else:
        mock_loop.run_in_executor = AsyncMock(side_effect=list(return_values))
    return mock_loop


def _make_loop_raising(exc):
    """Return a mock loop whose run_in_executor always raises exc."""
    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(side_effect=exc)
    return mock_loop


def _make_rom(rom_id, name, platform_name, platform_slug="gba"):
    """Build a minimal ROM dict as returned by the RomM API."""
    return {
        "id": rom_id,
        "name": name,
        "fs_name": f"{name}.zip",
        "platform_name": platform_name,
        "platform_slug": platform_slug,
    }


def _make_registry_entry(name, platform_name, app_id, platform_slug="gba"):
    """Build a minimal shortcut registry entry."""
    return {
        "app_id": app_id,
        "name": name,
        "fs_name": f"{name}.zip",
        "platform_name": platform_name,
        "platform_slug": platform_slug,
        "cover_path": "",
    }


def _page(items):
    """Wrap items in a paginated API response dict."""
    return {"items": items, "total": len(items)}
