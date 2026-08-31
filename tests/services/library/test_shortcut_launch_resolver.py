"""Tests for ShortcutLaunchResolver — it resolves each ROM's launch facts.

Driven through the shared ``plugin`` fixture so the emulator resolution runs
against the real :class:`ActiveCoreResolver` over the shared fake UoW — the same
seam the sync's bake sites draw from — rather than a mock of it.

The disc-resolved install paths (``do_scan_installed_paths`` /
``do_read_installed_paths``) are pinned in ``tests/services/test_disc_bake_sites.py``
alongside the other launch-bake sites, so a change to the disc pin's handling
fails every site at once. What is pinned HERE about those two is the boundary
that belongs to this module rather than to the disc pin: neither of them may
hold a Unit of Work open across the resolver's directory listing.
"""

from fakes.uow_open_probe import record_uow_open

from domain.shortcut_data import EmulatorInvocation

# conftest.py patches decky before this import
from tests.services.library._helpers import _seed_install


class TestBuildCoreOverrides:
    """The ``core_overrides`` map both preview and apply pass to ``build_shortcuts_data``.

    Maps ``rom_id -> resolved core_so`` for every ROM in the unit that carries a
    still-valid ``emulator_override``; NULL pins never enter the map, and a stale
    LABEL is omitted with a WARNING so the bake degrades to the plain launch.
    """

    def test_resolved_override_included_null_omitted(self, plugin):
        """A resolvable pin maps to its libretro EmulatorInvocation; an unpinned ROM is absent."""
        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]
        _seed_install(plugin, 10, file_path="/roms/psx/a.chd", platform_slug="psx")
        _seed_install(plugin, 11, file_path="/roms/psx/b.chd", platform_slug="psx")
        with plugin._uow:
            plugin._uow.roms.set_emulator_override(10, "PCSX ReARMed")

        roms = [{"id": 10, "platform_slug": "psx"}, {"id": 11, "platform_slug": "psx"}]
        result = plugin._sync_service._shortcut_launch_resolver.do_build_core_overrides(roms)

        assert result == {10: EmulatorInvocation.libretro("pcsx_rearmed_libretro", "PCSX ReARMed")}
        assert 11 not in result

    def test_stale_override_omitted_with_warning(self, plugin, caplog):
        """A pin whose LABEL no longer resolves is omitted and a WARNING is logged."""
        import logging

        plugin._core_info.available_cores = [
            {"core_so": "pcsx_rearmed_libretro", "label": "PCSX ReARMed", "is_default": True},
        ]
        _seed_install(plugin, 10, file_path="/roms/psx/a.chd", platform_slug="psx")
        with plugin._uow:
            plugin._uow.roms.set_emulator_override(10, "Removed Core")

        roms = [{"id": 10, "platform_slug": "psx"}]
        with caplog.at_level(logging.WARNING):
            result = plugin._sync_service._shortcut_launch_resolver.do_build_core_overrides(roms)

        assert result == {}
        assert "Removed Core" in caplog.text
        assert "no longer resolves" in caplog.text

    def test_no_overrides_returns_empty(self, plugin):
        """No pins anywhere → empty map (no available-cores lookups needed)."""
        _seed_install(plugin, 10, file_path="/roms/n64/a.z64", platform_slug="n64")
        result = plugin._sync_service._shortcut_launch_resolver.do_build_core_overrides(
            [{"id": 10, "platform_slug": "n64"}]
        )
        assert result == {}


class TestInstallPathReadsCloseTheUnitOfWorkFirst:
    """Neither install-path read holds a UoW open across the disc resolver.

    ``resolve_for_install`` lists the install directory, once per installed ROM.
    A UoW takes SQLite's ``BEGIN IMMEDIATE`` write lock, so a listing held
    inside one blocks every other writer in the plugin for the whole scan
    (CONTEXT.md → Unit of Work, #1779). ``FakeUnitOfWork`` shares no connection,
    so what a test can see is the ordering: the rows are snapshotted inside the
    transaction and every resolve runs after it closes.
    """

    def test_scan_resolves_after_the_unit_of_work_closes(self, plugin):
        _seed_install(plugin, 10, file_path="/roms/psx/a.chd", platform_slug="psx")
        _seed_install(plugin, 11, file_path="/roms/psx/b.chd", platform_slug="psx")
        resolver = plugin._sync_service._shortcut_launch_resolver
        open_at_resolve = record_uow_open(plugin._uow, resolver._disc_resolver, "resolve_for_install")

        paths = resolver.do_scan_installed_paths()

        assert set(paths) == {10, 11}
        assert open_at_resolve == [False, False]

    def test_read_resolves_after_the_unit_of_work_closes(self, plugin):
        _seed_install(plugin, 10, file_path="/roms/psx/a.chd", platform_slug="psx")
        _seed_install(plugin, 11, file_path="/roms/psx/b.chd", platform_slug="psx")
        resolver = plugin._sync_service._shortcut_launch_resolver
        open_at_resolve = record_uow_open(plugin._uow, resolver._disc_resolver, "resolve_for_install")

        paths = resolver.do_read_installed_paths({10, 11})

        assert set(paths) == {10, 11}
        assert open_at_resolve == [False, False]
