"""Tests for LocalLibraryReader — this device's own record of the library, read back out.

Driven through the shared ``plugin`` fixture so every read runs against the same
``FakeUnitOfWork`` the rest of the library suite seeds, rather than a mock of the
factory: what these methods return is only interesting relative to rows someone
actually persisted.

The projections are also exercised end-to-end where their consumers live — the
preview's cover-refresh and restamp counts in
``tests/services/library/test_sync_orchestrator.py``, the stale scan's
collision exclusion in the same file's late-ack reconciliation — so the tests
here pin the projection shapes those flows read through.
"""

from domain.work_unit import WorkUnit

# conftest.py patches decky before this import
from tests.services.library._helpers import _seed_rom_row


class TestRegistryProjections:
    """The two bound-row projections the classify/collapse passes diff against.

    Both carry the persisted ``cover_source`` fingerprint, so the cover-cache
    invalidation pass (#1386) scans the read the group collapse already made
    instead of opening per-ROM lookups.
    """

    _OLD = "/cover/big.png?ts=2026-01-01 00:00:00"

    def test_apply_registry_projection_carries_cover_source(self, plugin):
        # Round-trip: the bound-row projection the apply scan (and its group
        # collapse) reads must surface the persisted fingerprint.
        _seed_rom_row(
            plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64", cover_source=self._OLD
        )
        _seed_rom_row(plugin, 11, app_id=1011, platform_slug="n64", name="Null", fs_name="null.z64", cover_source=None)

        unit = WorkUnit(type="platform", id=1, name="N64", slug="n64", rom_count=2)
        registry = plugin._sync_service._local_library_reader.do_read_apply_registry(unit)

        assert registry["10"]["cover_source"] == self._OLD
        assert registry["11"]["cover_source"] is None

    def test_preview_baseline_projection_carries_cover_source(self, plugin):
        _seed_rom_row(
            plugin, 10, app_id=1010, platform_slug="n64", name="Keep", fs_name="keep.z64", cover_source=self._OLD
        )

        registry, _platforms, _collections = plugin._sync_service._local_library_reader.do_read_preview_baseline(
            {"n64": "N64"}
        )

        assert registry["10"]["cover_source"] == self._OLD


class TestResidentGroupKeys:
    """The DB's canonical sibling-group summaries the component keying seeds from (#1368)."""

    def test_read_resident_group_keys_filters_null_keys(self, plugin):
        _seed_rom_row(plugin, 1, app_id=100, platform_slug="n64", sibling_group_key="igdb:5:1")
        _seed_rom_row(plugin, 2, app_id=None, platform_slug="n64", sibling_group_key=None)

        keys = plugin._sync_service._local_library_reader.do_read_resident_group_keys()

        assert keys == {1: "igdb:5:1"}
