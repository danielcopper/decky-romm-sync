"""Shared helpers for the LibraryService sub-service test files.

Mock-loop factories for the executor pattern (LibraryService runs
sync RomM calls in an executor), small ROM/registry/page builders
used across :class:`TestFetchCollectionRoms`,
:class:`TestCollectionSyncEdgeCases`, and the facade-integration
collection tests, plus the shared-UoW seeders the sub-service test
files drive their fixtures with.
"""

from unittest.mock import AsyncMock, MagicMock

from services.library.fetcher import _SUPPORTED_VIRTUAL_TYPES


def rebind_loop(library_service, loop):
    """Rebind the event loop on every LibraryService sub-service.

    Four of the façade's sub-services (fetcher, orchestrator, reporter,
    session-budget monitor) hold their own ctor-bound ``_loop``. Tests that
    swap in a mock loop must propagate it to all four so async calls land on
    the override. ``ShortcutBakeInputs`` holds none — its methods are
    synchronous workers the orchestrator offloads through *its* loop.
    """
    library_service._fetcher._loop = loop
    library_service._orchestrator._loop = loop
    library_service._reporter._loop = loop
    library_service._session_budget._loop = loop


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


def _make_collections_loop(user=None, smart=None, virtual=None):
    """Mock loop matching the executor call order of ``get_collections`` /
    ``set_all_collections_sync`` (scope=None).

    Both fetch list_collections, then list_smart_collections, then one
    list_virtual_collections call per :data:`_SUPPORTED_VIRTUAL_TYPES` (in order).
    ``virtual`` is returned for the FIRST supported virtual type and ``[]`` for
    every other, so the whole virtual set is exactly ``virtual`` (tagged as the
    first type) — robust to the supported-type tuple growing.
    """
    values = [list(user or []), list(smart or [])]
    values.extend(list(virtual or []) if idx == 0 else [] for idx in range(len(_SUPPORTED_VIRTUAL_TYPES)))
    mock_loop = MagicMock()
    mock_loop.run_in_executor = AsyncMock(side_effect=values)
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


def _make_registry_entry(name, platform_name, app_id, platform_slug="gba", applied_launch_options=""):
    """Build a minimal shortcut registry entry.

    ``applied_launch_options`` defaults to ``""`` (the recorded uninstalled
    placeholder) so an identity-matching fetch with no ``launch_options`` reads as
    unchanged by the delta-restricted classify (#1383).
    """
    return {
        "app_id": app_id,
        "name": name,
        "fs_name": f"{name}.zip",
        "platform_name": platform_name,
        "platform_slug": platform_slug,
        "cover_path": "",
        "applied_launch_options": applied_launch_options,
    }


def _page(items):
    """Wrap items in a paginated API response dict."""
    return {"items": items, "total": len(items)}


def _seed_install(plugin, rom_id, *, file_path, platform_slug="n64"):
    """Insert a ``RomInstall`` record (with its FK-parent ``Rom``) into the shared UoW."""
    from domain.rom import Rom
    from domain.rom_install import RomInstall

    with plugin._uow:
        plugin._uow.roms.save(
            Rom(
                rom_id=rom_id,
                platform_slug=platform_slug,
                name=f"Game {rom_id}",
                fs_name=f"game_{rom_id}.z64",
                shortcut_app_id=None,
                last_synced_at="2025-01-01T00:00:00",
            )
        )
        plugin._uow.rom_installs.save(
            RomInstall.mark_installed(
                rom_id=rom_id,
                file_path=file_path,
                rom_dir=None,
                platform_slug=platform_slug,
                system=platform_slug,
                installed_at="2025-01-01T00:00:00",
            )
        )
