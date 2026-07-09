"""Contract tests for the paged metadata cache read callable.

``get_metadata_cache_page`` is driven exactly as ``src/api/backend.ts``
declares it — positional ``(offset, limit)`` numbers — and the assertions pin
the ``{items, total}`` response shape (the contract), not delegation. The
frontend pages this callable at plugin start so a large library never sends a
multi-MB dump through the size-limited callable bridge in one response (#1025).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.rom import Rom
from domain.rom_metadata import RomMetadata

if TYPE_CHECKING:
    from tests.contract._harness import ContractHarness


def _seed_metadata(harness: ContractHarness, rom_id: int, *, summary: str) -> None:
    """Seed a ``Rom`` FK anchor and its cached metadata in one transaction."""
    with harness.uow_factory() as uow:
        uow.roms.save(
            Rom.synced(
                rom_id=rom_id,
                platform_slug="gba",
                name=f"rom-{rom_id}",
                fs_name=f"rom-{rom_id}",
                shortcut_app_id=rom_id,
                synced_at="2026-01-01T00:00:00",
            )
        )
        uow.rom_metadata.save(
            rom_id,
            RomMetadata(
                summary=summary,
                genres=("RPG",),
                companies=(),
                first_release_date=None,
                average_rating=None,
                game_modes=(),
                player_count="1",
                cached_at=100.0,
            ),
        )


async def test_get_metadata_cache_page_paged_shape(harness):
    """Two rows across two pages: each page reports the full total; items are
    rom_id-ordered, disjoint, and carry the list-shaped wire entry."""
    _seed_metadata(harness, 1, summary="Game 1")
    _seed_metadata(harness, 2, summary="Game 2")

    first = await harness.plugin.get_metadata_cache_page(0, 1)
    assert set(first.keys()) == {"items", "total"}
    assert first["total"] == 2
    assert list(first["items"].keys()) == ["1"]
    entry = first["items"]["1"]
    assert entry["summary"] == "Game 1"
    # Tuple fields flatten to lists on the wire.
    assert entry["genres"] == ["RPG"]
    assert isinstance(entry["genres"], list)

    second = await harness.plugin.get_metadata_cache_page(1, 1)
    assert second["total"] == 2
    assert list(second["items"].keys()) == ["2"]


async def test_get_metadata_cache_page_out_of_range(harness):
    """An offset past the end returns empty items with the correct total — the
    frontend's page loop stops on the empty page while still knowing the count."""
    _seed_metadata(harness, 1, summary="Game 1")

    result = await harness.plugin.get_metadata_cache_page(500, 500)
    assert result == {"items": {}, "total": 1}


async def test_get_metadata_cache_page_empty_db(harness):
    """No cached rows → empty items, zero total."""
    result = await harness.plugin.get_metadata_cache_page(0, 500)
    assert result == {"items": {}, "total": 0}
