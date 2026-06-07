"""In-memory ``RomRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from domain.rom import Rom


class FakeRomRepository:
    """Dict-backed ``RomRepository`` keyed by ``rom_id``.

    Deep-copies on ``save`` so later in-memory mutations don't change what
    earlier ``save`` calls stored; ``save_count`` lets tests assert writes.
    """

    def __init__(self) -> None:
        self._roms: dict[int, Rom] = {}
        self.save_count = 0

    def get(self, rom_id: int) -> Rom | None:
        return copy.deepcopy(self._roms.get(rom_id))

    def get_by_app_id(self, app_id: int) -> Rom | None:
        for rom in self._roms.values():
            if rom.shortcut_app_id == app_id:
                return copy.deepcopy(rom)
        return None

    def save(self, rom: Rom) -> None:
        self.save_count += 1
        self._roms[rom.rom_id] = copy.deepcopy(rom)

    def delete(self, rom_id: int) -> None:
        self._roms.pop(rom_id, None)

    def iter_all(self) -> Iterator[Rom]:
        return iter([copy.deepcopy(rom) for rom in self._roms.values()])

    def iter_by_platform(self, platform_slug: str) -> Iterator[Rom]:
        return iter([copy.deepcopy(rom) for rom in self._roms.values() if rom.platform_slug == platform_slug])

    def count(self) -> int:
        return len(self._roms)

    def set_emulator_override(self, rom_id: int, label: str | None) -> None:
        rom = self._roms.get(rom_id)
        if rom is not None:
            rom.emulator_override = label

    def get_all_emulator_overrides(self) -> dict[int, str]:
        return {
            rom_id: rom.emulator_override for rom_id, rom in self._roms.items() if rom.emulator_override is not None
        }

    def _snapshot(self) -> dict[int, Rom]:
        return copy.deepcopy(self._roms)

    def _restore(self, state: dict[int, Rom]) -> None:
        self._roms = state
