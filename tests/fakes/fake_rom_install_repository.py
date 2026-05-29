"""In-memory ``RomInstallRepository`` implementation for service tests."""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from domain.rom_install import RomInstall


class FakeRomInstallRepository:
    """Dict-backed ``RomInstallRepository`` keyed by ``rom_id``."""

    def __init__(self) -> None:
        self._installs: dict[int, RomInstall] = {}
        self.save_count = 0

    def get(self, rom_id: int) -> RomInstall | None:
        return copy.deepcopy(self._installs.get(rom_id))

    def save(self, install: RomInstall) -> None:
        self.save_count += 1
        self._installs[install.rom_id] = copy.deepcopy(install)

    def delete(self, rom_id: int) -> None:
        self._installs.pop(rom_id, None)

    def iter_all(self) -> Iterator[RomInstall]:
        return iter([copy.deepcopy(install) for install in self._installs.values()])

    def _snapshot(self) -> dict[int, RomInstall]:
        return copy.deepcopy(self._installs)

    def _restore(self, state: dict[int, RomInstall]) -> None:
        self._installs = state
