from __future__ import annotations

import json
from typing import Any, cast

from fakes.fake_unit_of_work import FakeUnitOfWork, FakeUnitOfWorkFactory

from domain.platform_sync_state import PlatformSyncState
from domain.rom import Rom
from domain.version_metadata import VersionMetadata
from services.prune._models import PrunePreview
from services.prune.preview import PreviewBuilder, PreviewBuilderConfig


class _Recovery:
    def free_bytes(self) -> int:
        return 1000

    def root(self) -> str:
        return "/recovery"


class _Paths:
    def roms_path(self) -> str:
        return "/roms"


def _rom(rom_id: int, *, name: str, group: str, fetch_id: str | None) -> Rom:
    row = Rom.synced(
        rom_id=rom_id,
        platform_slug="dc",
        name=name,
        fs_name=f"{name}.chd",
        shortcut_app_id=None,
        synced_at="now",
        version=VersionMetadata(sibling_group_key=group),
    )
    if fetch_id is not None:
        row.record_fetch_generation(fetch_id)
    return row


def _builder(uow: FakeUnitOfWork) -> PreviewBuilder:
    return PreviewBuilder(
        config=PreviewBuilderConfig(
            uow_factory=FakeUnitOfWorkFactory(uow),
            recovery_store=cast("Any", _Recovery()),
            retrodeck_paths=cast("Any", _Paths()),
            settings={},
        )
    )


def _two_groups() -> FakeUnitOfWork:
    """Two sibling groups, each a dropped row beside a generation-current one."""
    uow = FakeUnitOfWork()
    with uow:
        uow.roms.save(_rom(4375, name="Game A", group="igdb:1217:53", fetch_id="old"))
        uow.roms.save(_rom(25135, name="Game A", group="igdb:1217:53", fetch_id="current"))
        uow.roms.save(_rom(4376, name="Game B", group="igdb:1218:53", fetch_id="old"))
        uow.roms.save(_rom(25136, name="Game B", group="igdb:1218:53", fetch_id="current"))
        uow.platform_sync_state.save(
            PlatformSyncState.stamp(platform_slug="dc", at="now", rom_count=2, fetch_id="current")
        )
    return uow


def test_page_counts_only_removable_rows_as_candidates() -> None:
    builder = _builder(_two_groups())

    preview = builder.build("preview", "bulk", None)
    page = builder.page(preview, 0, 50)

    # Every group member stays disclosed — a fresh probe, not the local fetch
    # generation, decides whole-game removal, so a generation-current row can
    # still be taken and may never be deleted unseen.
    assert page["total"] == 4
    # ...but only the dropped rows are what this run can remove on its own.
    assert page["candidate_total"] == 2
    assert {item["rom_id"] for item in page["items"] if item["candidate"]} == {4375, 4376}
    assert {item["rom_id"] for item in page["items"] if not item["candidate"]} == {25135, 25136}


def test_page_orders_candidates_ahead_of_disclosed_siblings() -> None:
    builder = _builder(_two_groups())

    preview = builder.build("preview", "bulk", None)
    page = builder.page(preview, 0, 50)

    assert [item["rom_id"] for item in page["items"]] == [4375, 4376, 25135, 25136]


def test_page_reports_a_candidate_total_the_first_window_cannot_see() -> None:
    builder = _builder(_two_groups())

    preview = builder.build("preview", "bulk", None)
    first = builder.page(preview, 0, 1)

    # The headline count must be right before the list has been paged through.
    assert len(first["items"]) == 1
    assert (first["total"], first["candidate_total"]) == (4, 2)


def test_empty_page_still_carries_both_counts() -> None:
    builder = _builder(_two_groups())

    preview = builder.build("preview", "bulk", None)
    refreshed = builder.page(preview, 0, 0)

    # The free-space refresh asks for limit=0 and must not blank the counts.
    assert refreshed["items"] == []
    assert (refreshed["total"], refreshed["candidate_total"]) == (4, 2)


def test_preview_pages_stay_within_wire_budget_for_non_ascii_rows() -> None:
    builder = PreviewBuilder(
        config=PreviewBuilderConfig(
            uow_factory=cast("Any", None),
            recovery_store=cast("Any", _Recovery()),
            retrodeck_paths=cast("Any", None),
            settings={},
        )
    )
    entries = tuple(
        {
            "rom_id": index,
            "name": "é" * 512,
            "name_truncated": True,
            "fs_name": "遊" * 512,
            "fs_name_truncated": True,
            "platform_slug": "platform",
            "group_id": "é" * 512,
            "group_id_truncated": True,
            "group_size": 1,
            "bound_count": 0,
            "candidate": True,
            "installed": True,
            "installed_bytes": 1,
            "warning": "遊" * 1024,
            "warning_truncated": True,
        }
        for index in range(1, 51)
    )
    preview = PrunePreview("preview", "bulk", None, frozenset(range(1, 51)), (), entries, 1000, "server|user")
    offset = 0
    seen: list[int] = []

    while offset < len(entries):
        page = builder.page(preview, offset, 50)
        assert len(json.dumps(page, ensure_ascii=True).encode("utf-8")) <= 48 * 1024
        ids = [item["rom_id"] for item in page["items"]]
        assert ids
        seen.extend(ids)
        offset += len(ids)

    assert seen == list(range(1, 51))
