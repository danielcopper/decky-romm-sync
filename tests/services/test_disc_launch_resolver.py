"""Tests for DiscLaunchResolver — the single read-path disc-resolution seam.

Covers enumeration (single-file → empty, folder-backed → disc list, es_systems
intersection, fallback when es_systems is unavailable) and bake-path resolution
(non-multi-disc → file_path unchanged, pinned → that disc, stale pin → default +
WARNING).
"""

from __future__ import annotations

import logging

import pytest

from domain.rom_install import RomInstall
from services.disc_launch_resolver import DiscLaunchResolver, DiscLaunchResolverConfig


class FakeFileLister:
    """In-memory ``DirectoryFileListerFn`` — maps a directory to its file paths."""

    def __init__(self, files_by_dir: dict[str, list[str]] | None = None) -> None:
        self.files_by_dir = files_by_dir if files_by_dir is not None else {}
        self.calls: list[str] = []

    def __call__(self, directory: str) -> list[str]:
        self.calls.append(directory)
        return list(self.files_by_dir.get(directory, []))


class FakeSystemExtensions:
    """In-memory ``SystemSupportedExtensionsFn`` — maps a system to its accept-list."""

    def __init__(self, by_system: dict[str, frozenset[str]] | None = None) -> None:
        self.by_system = by_system if by_system is not None else {}

    def __call__(self, system_name: str) -> frozenset[str]:
        return self.by_system.get(system_name, frozenset())


def _install(*, rom_id: int = 1, file_path: str, rom_dir: str | None, system: str = "psx") -> RomInstall:
    return RomInstall(
        rom_id=rom_id,
        file_path=file_path,
        rom_dir=rom_dir,
        platform_slug=system,
        system=system,
        installed_at="2026-01-01T00:00:00+00:00",
    )


@pytest.fixture
def logger() -> logging.Logger:
    return logging.getLogger("test_disc_launch_resolver")


def _build(file_lister: FakeFileLister, system_extensions: FakeSystemExtensions, logger: logging.Logger):
    return DiscLaunchResolver(
        config=DiscLaunchResolverConfig(
            list_files=file_lister,
            system_extensions=system_extensions,
            logger=logger,
        ),
    )


class TestEnumerateDiscs:
    def test_single_file_rom_enumerates_empty(self, logger):
        # rom_dir is None → single-file ROM owns no folder, no second disc.
        resolver = _build(FakeFileLister(), FakeSystemExtensions(), logger)
        install = _install(file_path="/roms/psx/game.chd", rom_dir=None)
        assert resolver.enumerate_discs(install) == []

    def test_folder_backed_lists_discs_in_order(self, logger):
        files = {
            "/roms/psx/game": [
                "/roms/psx/game/Game (Disc 2).cue",
                "/roms/psx/game/Game (Disc 1).cue",
            ]
        }
        resolver = _build(FakeFileLister(files), FakeSystemExtensions(), logger)
        install = _install(file_path="/roms/psx/game/Game (Disc 1).cue", rom_dir="/roms/psx/game")
        discs = resolver.enumerate_discs(install)
        assert [d.filename for d in discs] == ["Game (Disc 1).cue", "Game (Disc 2).cue"]
        assert [d.index for d in discs] == [1, 2]

    def test_es_systems_intersection_excludes_unsupported_format(self, logger):
        # System accepts only .iso → a .cue disc is dropped by the intersection.
        files = {"/roms/x/game": ["/roms/x/game/d.iso", "/roms/x/game/d.cue"]}
        system_extensions = FakeSystemExtensions({"xbox": frozenset({".iso"})})
        resolver = _build(FakeFileLister(files), system_extensions, logger)
        install = _install(file_path="/roms/x/game/d.iso", rom_dir="/roms/x/game", system="xbox")
        discs = resolver.enumerate_discs(install)
        assert [d.filename for d in discs] == ["d.iso"]

    def test_empty_es_systems_falls_back_to_full_disc_set(self, logger):
        # Unknown system → empty accept-list → fall back to full disc set, so
        # both the .cue and .chd are kept rather than intersecting to nothing.
        files = {"/roms/x/game": ["/roms/x/game/a.cue", "/roms/x/game/b.chd"]}
        resolver = _build(FakeFileLister(files), FakeSystemExtensions(), logger)
        install = _install(file_path="/roms/x/game/a.cue", rom_dir="/roms/x/game", system="unknown")
        discs = resolver.enumerate_discs(install)
        assert {d.filename for d in discs} == {"a.cue", "b.chd"}


class TestResolveBakePath:
    def test_non_multi_disc_resolves_to_file_path_unchanged(self, logger):
        # Fewer than two discs → resolve_launch_path returns file_path, no scan
        # override. This is the zero-behavior-change guarantee for single-disc.
        resolver = _build(FakeFileLister(), FakeSystemExtensions(), logger)
        install = _install(file_path="/roms/psx/game.chd", rom_dir=None)
        assert resolver.resolve_bake_path(install, [], None) == "/roms/psx/game.chd"

    def test_pinned_disc_resolves_to_that_disc_path(self, logger):
        files = {
            "/roms/psx/game": [
                "/roms/psx/game/Game (Disc 1).cue",
                "/roms/psx/game/Game (Disc 2).cue",
            ]
        }
        resolver = _build(FakeFileLister(files), FakeSystemExtensions(), logger)
        install = _install(file_path="/roms/psx/game/Game (Disc 1).cue", rom_dir="/roms/psx/game")
        discs = resolver.enumerate_discs(install)
        path = resolver.resolve_bake_path(install, discs, "Game (Disc 2).cue")
        assert path == "/roms/psx/game/Game (Disc 2).cue"

    def test_stale_pin_degrades_to_default_and_warns(self, caplog, logger):
        files = {
            "/roms/psx/game": [
                "/roms/psx/game/Game (Disc 1).cue",
                "/roms/psx/game/Game (Disc 2).cue",
            ]
        }
        resolver = _build(FakeFileLister(files), FakeSystemExtensions(), logger)
        install = _install(file_path="/roms/psx/game/Game (Disc 1).cue", rom_dir="/roms/psx/game")
        discs = resolver.enumerate_discs(install)
        with caplog.at_level(logging.WARNING, logger="test_disc_launch_resolver"):
            path = resolver.resolve_bake_path(install, discs, "Game (Disc 9).cue")
        # Missing pin degrades to disc 1 (the default), never fatal.
        assert path == "/roms/psx/game/Game (Disc 1).cue"
        assert any("no longer present" in r.message for r in caplog.records)

    def test_valid_pin_does_not_warn(self, caplog, logger):
        files = {
            "/roms/psx/game": [
                "/roms/psx/game/Game (Disc 1).cue",
                "/roms/psx/game/Game (Disc 2).cue",
            ]
        }
        resolver = _build(FakeFileLister(files), FakeSystemExtensions(), logger)
        install = _install(file_path="/roms/psx/game/Game (Disc 1).cue", rom_dir="/roms/psx/game")
        discs = resolver.enumerate_discs(install)
        with caplog.at_level(logging.WARNING, logger="test_disc_launch_resolver"):
            resolver.resolve_bake_path(install, discs, "Game (Disc 2).cue")
        assert not any("no longer present" in r.message for r in caplog.records)


class TestResolveForInstall:
    def test_combines_enumerate_and_resolve(self, logger):
        files = {
            "/roms/psx/game": [
                "/roms/psx/game/Game (Disc 1).cue",
                "/roms/psx/game/Game (Disc 2).cue",
            ]
        }
        resolver = _build(FakeFileLister(files), FakeSystemExtensions(), logger)
        install = _install(file_path="/roms/psx/game/Game (Disc 1).cue", rom_dir="/roms/psx/game")
        # No pin → default is disc 1 (file_path is not .m3u).
        assert resolver.resolve_for_install(install, None) == "/roms/psx/game/Game (Disc 1).cue"
        # Pin disc 2 → that disc's path.
        assert resolver.resolve_for_install(install, "Game (Disc 2).cue") == "/roms/psx/game/Game (Disc 2).cue"

    def test_m3u_install_default_keeps_m3u(self, logger):
        files = {
            "/roms/psx/game": [
                "/roms/psx/game/Game.m3u",
                "/roms/psx/game/Game (Disc 1).cue",
                "/roms/psx/game/Game (Disc 2).cue",
            ]
        }
        system_extensions = FakeSystemExtensions({"psx": frozenset({".cue", ".chd", ".m3u"})})
        resolver = _build(FakeFileLister(files), system_extensions, logger)
        install = _install(file_path="/roms/psx/game/Game.m3u", rom_dir="/roms/psx/game")
        # file_path is the .m3u → NULL-selection default stays the m3u (the
        # in-emulator disc-swap playlist), not disc 1.
        assert resolver.resolve_for_install(install, None) == "/roms/psx/game/Game.m3u"

    def test_single_file_install_resolves_to_file_path(self, logger):
        resolver = _build(FakeFileLister(), FakeSystemExtensions(), logger)
        install = _install(file_path="/roms/snes/game.sfc", rom_dir=None)
        assert resolver.resolve_for_install(install, None) == "/roms/snes/game.sfc"
