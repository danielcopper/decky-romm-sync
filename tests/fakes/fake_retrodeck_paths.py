"""In-memory ``RetroDeckPaths`` implementation for service tests."""

from __future__ import annotations


class FakeRetroDeckPaths:
    """In-memory ``RetroDeckPaths`` for tests.

    Each path is a mutable attribute so tests can flip individual
    directories without rebuilding the whole bundle. Defaults to empty
    strings, matching the production fallback when ``retrodeck.json``
    is absent.
    """

    def __init__(
        self,
        *,
        saves: str = "",
        roms: str = "",
        bios: str = "",
        home: str = "",
    ) -> None:
        self.saves = saves
        self.roms = roms
        self.bios = bios
        self.home = home

    def saves_path(self) -> str:
        return self.saves

    def roms_path(self) -> str:
        return self.roms

    def bios_path(self) -> str:
        return self.bios

    def retrodeck_home(self) -> str:
        return self.home
