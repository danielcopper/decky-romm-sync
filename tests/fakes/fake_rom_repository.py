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
        # Mirror production's collision-safe bind (rom.py save + the 003 partial
        # unique index): a bound appId belongs to at most one row, so unbind any
        # SIBLING row holding it first (ADR-0007 — unbind, never delete). The
        # rom_id guard keeps a same-rom re-save idempotent. Without this the
        # dict store would silently keep two rows sharing one appId and diverge
        # from real SQLite (#1036).
        if rom.shortcut_app_id is not None:
            for other_id, other in self._roms.items():
                if other_id != rom.rom_id and other.shortcut_app_id == rom.shortcut_app_id:
                    other.unbind_shortcut()
        stored = copy.deepcopy(rom)
        # Mirror SqliteRomRepository.save(): the pin-only columns
        # (emulator_override / selected_disc / applied_launch_options) are EXCLUDED
        # from the sync UPSERT, so a re-save preserves the existing row's values
        # rather than overwriting them from the (fresh, default-None) incoming Rom.
        # Each is written only by its own set_*() path. Without this the dict store
        # would wipe a user's pin / the recorded applied state on every re-sync and
        # diverge from real SQLite (#945 / #1383).
        existing = self._roms.get(rom.rom_id)
        if existing is not None:
            stored.emulator_override = existing.emulator_override
            stored.selected_disc = existing.selected_disc
            stored.applied_launch_options = existing.applied_launch_options
        self._roms[rom.rom_id] = stored

    def delete(self, rom_id: int) -> None:
        self._roms.pop(rom_id, None)

    def iter_all(self) -> Iterator[Rom]:
        return iter([copy.deepcopy(rom) for rom in self._roms.values()])

    def iter_by_platform(self, platform_slug: str) -> Iterator[Rom]:
        return iter([copy.deepcopy(rom) for rom in self._roms.values() if rom.platform_slug == platform_slug])

    def iter_by_group_key(self, group_key: str) -> Iterator[Rom]:
        # Mirrors the SQLite ``WHERE sibling_group_key = ?``: a NULL key never
        # matches, so an unbackfilled / solo row is never returned by this path.
        return iter([copy.deepcopy(rom) for rom in self._roms.values() if rom.sibling_group_key == group_key])

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

    def set_selected_disc(self, rom_id: int, filename: str | None) -> None:
        rom = self._roms.get(rom_id)
        if rom is not None:
            rom.selected_disc = filename

    def set_applied_launch_options(self, rom_id: int, launch_options: str | None) -> None:
        rom = self._roms.get(rom_id)
        if rom is not None:
            rom.applied_launch_options = launch_options

    def clear_all_applied_launch_options(self) -> None:
        for rom in self._roms.values():
            rom.applied_launch_options = None

    def _snapshot(self) -> dict[int, Rom]:
        return copy.deepcopy(self._roms)

    def _restore(self, state: dict[int, Rom]) -> None:
        self._roms = state
