"""DiscLaunchResolver — the single read-path disc-resolution seam per ROM.

The one place that answers "which file will this multi-disc ROM actually launch
with?", folding the user's persisted ``roms.selected_disc`` pick over the live
enumeration of disc images in the ROM's install directory. Every launch-bake
site draws the bake path from this seam so the baked launch_options never
diverge from the picker's current selection, and the picker callables enumerate
through the same seam so the list they show is the list the bake resolves over.

Resolution is a bake-time path-override layer only: it returns the path to bake
into the Steam shortcut's launch_options, it never rewrites the install's
``file_path`` (save-path / core / displayed-filename derivations stay stable),
mirroring how :class:`ActiveCoreResolver` overrides the invocation without
touching ``file_path``. A non-multi-disc ROM (single-file install, or a folder
with fewer than two disc images) resolves to its own ``file_path`` unchanged —
zero behavior change. A stale pin (the selected disc is no longer present)
degrades to the default with a WARNING, never fatal, mirroring
``ActiveCoreResolver``'s stale-label handling. See ADR-0014.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.disc_selection import Disc, enumerate_discs, resolve_launch_path

if TYPE_CHECKING:
    import logging

    from domain.rom_install import RomInstall
    from services.protocols import (
        DirectoryFileListerFn,
        SystemSupportedExtensionsFn,
    )


@dataclass(frozen=True)
class DiscLaunchResolverConfig:
    """Frozen wiring bundle handed to ``DiscLaunchResolver.__init__``.

    Carries the recursive directory file lister (to scan a folder-backed ROM's
    install directory for disc images), the live ES-DE per-system supported
    extensions reader (intersected with the disc-image set so a disc the
    emulator cannot launch is never offered), and the logger used to warn on a
    stale pin.
    """

    list_files: DirectoryFileListerFn
    system_extensions: SystemSupportedExtensionsFn
    logger: logging.Logger


class DiscLaunchResolver:
    """Resolve the launch-bake path and disc list for one installed ROM."""

    def __init__(self, *, config: DiscLaunchResolverConfig) -> None:
        self._list_files = config.list_files
        self._system_extensions = config.system_extensions
        self._logger = config.logger

    def enumerate_discs(self, install: RomInstall) -> list[Disc]:
        """Enumerate the launchable discs in *install*'s directory, in disc order.

        A single-file ROM (``rom_dir is None``) owns no folder and can hold no
        second disc, so it enumerates to an empty list. A folder-backed ROM is
        scanned recursively; the live ES-DE accept-list for its system is
        intersected with the disc-image set so a disc the emulator cannot launch
        is never listed (falling back to the full disc set when ES-DE is
        unavailable). Pure file listing — no mutation.
        """
        if install.rom_dir is None:
            return []
        files = self._list_files(install.rom_dir)
        supported = self._system_extensions(install.system)
        # An empty accept-list means ES-DE could not answer; fall back to the
        # full disc set (pass None) rather than intersecting to nothing.
        return enumerate_discs(files, supported or None)

    def resolve_bake_path(self, install: RomInstall, discs: list[Disc], selected_disc: str | None) -> str:
        """Return the path to bake into launch_options for *install*.

        *discs* is the result of :meth:`enumerate_discs` (passed in so a caller
        that already enumerated for the picker does not re-scan). Resolves the
        persisted *selected_disc* over the disc list: a non-multi-disc ROM and an
        unpinned ROM both resolve to the default (the ``.m3u`` when ``file_path``
        is one, else disc 1), a valid pin resolves to that disc, and a stale pin
        degrades to the default with a WARNING — never fatal, never a bogus path.
        """
        path, stale = resolve_launch_path(install.file_path, discs, selected_disc)
        if stale:
            self._logger.warning(
                "disc_launch_resolver: pinned disc '%s' for rom_id=%s is no longer present; "
                "degrading to the default launch target",
                selected_disc,
                install.rom_id,
            )
        return path

    def resolve_for_install(self, install: RomInstall, selected_disc: str | None) -> str:
        """Enumerate *install* and resolve the launch-bake path in one call.

        The bake-site convenience wrapper: enumerate the discs, then resolve the
        *selected_disc* pin over them. Equivalent to :meth:`enumerate_discs`
        followed by :meth:`resolve_bake_path`; callers that also need the disc
        list (the picker) call the two halves directly to avoid re-scanning.
        """
        discs = self.enumerate_discs(install)
        return self.resolve_bake_path(install, discs, selected_disc)
