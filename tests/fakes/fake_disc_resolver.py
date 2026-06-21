"""In-memory ``DiscResolver`` implementation for service tests.

Lets the launch-bake consumers (library sync, download-complete, RetroDECK-home
migration) and the disc-picker callables inject the disc-resolution seam without
standing up a real ``DiscLaunchResolver`` (directory scan + es_systems read +
``domain.disc_selection``). Configure per-``rom_dir`` disc lists; the default
(no discs seeded for a directory) reproduces a single-disc ROM, which resolves to
its own ``file_path`` unchanged — matching the real resolver's zero-behavior-
change contract for non-multi-disc ROMs. Each resolve call is recorded so a
consumer test can assert the seam was reached with the right pin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from domain.disc_selection import resolve_launch_path

if TYPE_CHECKING:
    from domain.disc_selection import Disc
    from domain.rom_install import RomInstall


class FakeDiscResolver:
    """Maps an install's ``rom_dir`` to a configured disc list for tests.

    Seed a directory's discs via ``set_discs(rom_dir, discs)``; a directory with
    no seeded discs enumerates empty (single-disc ROM). ``resolve_bake_path`` and
    ``resolve_for_install`` mirror the real resolver's
    :func:`domain.disc_selection.resolve_launch_path` decision over the seeded
    discs, so a non-multi-disc ROM resolves to ``install.file_path`` unchanged and
    a pin resolves to the matching disc's path. ``calls`` records each
    ``(rom_dir, selected_disc)`` resolve so consumer tests can assert the seam
    was queried with the right pin.
    """

    def __init__(self) -> None:
        self._by_rom_dir: dict[str | None, list[Disc]] = {}
        self.calls: list[tuple[str | None, str | None]] = []

    def set_discs(self, rom_dir: str, discs: list[Disc]) -> None:
        """Seed the enumerated disc list for installs whose ``rom_dir`` matches."""
        self._by_rom_dir[rom_dir] = discs

    def enumerate_discs(self, install: RomInstall) -> list[Disc]:
        if install.rom_dir is None:
            return []
        return list(self._by_rom_dir.get(install.rom_dir, []))

    def resolve_bake_path(self, install: RomInstall, discs: list[Disc], selected_disc: str | None) -> str:
        self.calls.append((install.rom_dir, selected_disc))
        path, _stale = resolve_launch_path(install.file_path, discs, selected_disc)
        return path

    def resolve_for_install(self, install: RomInstall, selected_disc: str | None) -> str:
        return self.resolve_bake_path(install, self.enumerate_discs(install), selected_disc)
