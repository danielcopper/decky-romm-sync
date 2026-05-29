"""In-memory ``RomMetadataRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from domain.rom_metadata import RomMetadata


class FakeRomMetadataRepository:
    """Dict-backed ``RomMetadataRepository`` keyed by externally-supplied ``rom_id``."""

    def __init__(self) -> None:
        self._metadata: dict[int, RomMetadata] = {}
        self.save_count = 0

    def get(self, rom_id: int) -> RomMetadata | None:
        return copy.deepcopy(self._metadata.get(rom_id))

    def save(self, rom_id: int, metadata: RomMetadata) -> None:
        self.save_count += 1
        self._metadata[rom_id] = copy.deepcopy(metadata)

    def delete(self, rom_id: int) -> None:
        self._metadata.pop(rom_id, None)

    def _snapshot(self) -> dict[int, RomMetadata]:
        return copy.deepcopy(self._metadata)

    def _restore(self, state: dict[int, RomMetadata]) -> None:
        self._metadata = state
