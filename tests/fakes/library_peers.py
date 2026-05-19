"""In-memory peer-service fakes consumed by the LibraryService test suite.

Provides minimal ``MetadataExtractor`` and ``ArtworkManager`` Protocol
implementations for tests that wire LibraryService (or its sub-services)
without exercising metadata extraction or the SteamGridDB artwork
pipeline. Both fakes record calls so tests that DO care about wiring
can assert on the recorded activity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from models.state import MetadataCacheEntry, ShortcutRegistryEntry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class FakeMetadataExtractor:
    """In-memory ``MetadataExtractor`` for tests.

    Returns the dict configured at construction (``canned_extract``) from
    every ``extract_metadata`` call. Counts ``mark_metadata_dirty``,
    ``flush_metadata_if_dirty``, and ``record_unit_metadata`` invocations
    so tests that wire the peer can assert it was reached without
    standing up the real service.
    """

    def __init__(self, canned_extract: dict | None = None) -> None:
        self.canned_extract: dict = canned_extract if canned_extract is not None else {}
        self.extract_calls: list[dict] = []
        self.mark_dirty_count: int = 0
        self.flush_count: int = 0
        self.record_unit_calls: list[list[dict]] = []

    def extract_metadata(self, rom: dict) -> MetadataCacheEntry:
        self.extract_calls.append(rom)
        return cast("MetadataCacheEntry", dict(self.canned_extract))

    def mark_metadata_dirty(self) -> None:
        self.mark_dirty_count += 1

    def flush_metadata_if_dirty(self) -> None:
        self.flush_count += 1

    def record_unit_metadata(self, roms: list[dict]) -> None:
        self.record_unit_calls.append(list(roms))


class FakeArtworkManager:
    """In-memory ``ArtworkManager`` for tests.

    ``download_artwork`` returns the dict configured at construction
    (``canned_download``) and records the call args.
    ``finalize_cover_path`` passes the input ``cover_path`` through
    unchanged by default — tests that need a rewrite can override the
    callable via ``finalize_override``.
    ``remove_artwork_files`` records each call so tests can assert
    removal was triggered without standing up the real service.
    """

    def __init__(
        self,
        canned_download: dict | None = None,
        finalize_override: Callable[[str | None, str, int, str], str] | None = None,
    ) -> None:
        self.canned_download: dict = canned_download if canned_download is not None else {}
        self.finalize_override = finalize_override
        self.download_calls: list[tuple[list[dict], Any, Any, int, int]] = []
        self.finalize_calls: list[tuple[str | None, str, int, str]] = []
        self.remove_calls: list[tuple[str, str | int, ShortcutRegistryEntry]] = []

    async def download_artwork(
        self,
        all_roms: list[dict],
        emit_progress: Awaitable[None] | Callable[..., Awaitable[None]],
        is_cancelling: Any,
        progress_step: int = 4,
        progress_total_steps: int = 6,
    ) -> dict:
        self.download_calls.append((list(all_roms), emit_progress, is_cancelling, progress_step, progress_total_steps))
        return dict(self.canned_download)

    def finalize_cover_path(self, grid: str | None, cover_path: str, app_id: int, rom_id_str: str) -> str:
        self.finalize_calls.append((grid, cover_path, app_id, rom_id_str))
        if self.finalize_override is not None:
            return self.finalize_override(grid, cover_path, app_id, rom_id_str)
        return cover_path

    def remove_artwork_files(self, grid: str, rom_id: str | int, entry: ShortcutRegistryEntry) -> None:
        self.remove_calls.append((grid, rom_id, cast("ShortcutRegistryEntry", dict(entry))))
