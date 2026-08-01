"""Save locks held over an ownership set that is proven stable while holding them.

Which local ROMs own a purge set's save paths is only knowable by asking the
saves context, and the answer can change between asking and locking. Anything
that has to act on save files under a lock goes through here, so no phase can
mutate a save whose ownership widened after the lock set was chosen.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import asyncio
    from collections.abc import AsyncIterator

    from services.protocols import PruneSaveCoordinator

_STABLE_LOCK_ATTEMPTS = 3


@dataclass(frozen=True)
class SaveLockCoordinatorConfig:
    """Dependencies for acquiring save locks over a stable ownership set."""

    loop: asyncio.AbstractEventLoop
    save_coordinator: PruneSaveCoordinator


class SaveLockCoordinator:
    """Hold the save locks for an ownership set that did not move under them."""

    def __init__(self, *, config: SaveLockCoordinatorConfig) -> None:
        self._loop = config.loop
        self._save_coordinator = config.save_coordinator

    @contextlib.asynccontextmanager
    async def stable_locks(self, rom_ids: set[int]) -> AsyncIterator[dict[str, Any]]:
        """Yield the save inventory whose owner set the held locks still cover.

        Re-reads ownership once the locks are held and retries when it changed,
        so the yielded inventory is the one the locks actually protect. Raises
        when ownership will not settle rather than acting on a stale set.
        """
        requested = sorted(rom_ids)
        for _attempt in range(_STABLE_LOCK_ATTEMPTS):
            first = await self.inventory(requested)
            lock_ids = sorted({int(value) for value in first.get("lock_rom_ids", requested)})
            async with self._save_coordinator.lock_prune_roms(lock_ids):
                current = await self.inventory(requested)
                current_locks = sorted({int(value) for value in current.get("lock_rom_ids", requested)})
                if current_locks == lock_ids:
                    yield current
                    return
        raise RuntimeError("Save ownership kept changing while cleanup acquired locks")

    async def inventory(self, rom_ids: list[int]) -> dict[str, Any]:
        """Read exact-path save ownership for *rom_ids* off the loop thread."""
        return await self._loop.run_in_executor(None, self._save_coordinator.inventory_prune_saves, rom_ids)

    @staticmethod
    def inventory_warnings(inventory: dict[str, Any]) -> list[str]:
        """The inventory's user-facing warnings, coerced to strings."""
        raw = inventory.get("warnings")
        return [str(item) for item in raw] if isinstance(raw, list) else []


__all__ = ["SaveLockCoordinator", "SaveLockCoordinatorConfig"]
