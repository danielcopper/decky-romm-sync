"""RetroDECK runtime path, system, and core resolution Protocols.

Services query the host RetroDECK/RetroArch/ES-DE environment through
these Protocols: filesystem path getters (saves, roms, BIOS,
RetroDECK home), platform-to-system resolution, RetroArch save sorting
toggles, and RetroArch core lookups for ES-DE configured systems.
``GamelistXmlEditorProtocol`` is the matching write seam for ES-DE
per-system / per-game core overrides — paired with ``CoreInfoProvider``
which owns the read side.
"""

from __future__ import annotations

from typing import Protocol


class SystemResolver(Protocol):
    """Resolve a RomM platform slug to a RetroDECK system path."""

    def __call__(self, platform_slug: str, platform_fs_slug: str | None = None) -> str: ...


class RetroDeckPaths(Protocol):
    """Bundled accessor for the four RetroDECK runtime directory paths.

    Distinct method names per path are deliberate: a single
    ``def __call__(self) -> str`` shape would make a saves-for-bios
    mix-up silently type-check at the call site. Separate names give
    the type checker enough information to flag it.
    """

    def saves_path(self) -> str: ...

    def roms_path(self) -> str: ...

    def bios_path(self) -> str: ...

    def retrodeck_home(self) -> str: ...


class RetroArchSaveSortingProvider(Protocol):
    """Return RetroArch save sorting settings as (sort_by_content, sort_by_core)."""

    def __call__(self) -> tuple[bool, bool]: ...


class CoreResolverFn(Protocol):
    """Resolve the active RetroArch core for a system/game."""

    def __call__(self, system_name: str, rom_filename: str | None = None) -> tuple[str | None, str | None]: ...


class CoreInfoProvider(Protocol):
    """Core resolution for ES-DE configured systems, consumed by services.

    Exposes the read seam services need to ask "which RetroArch core is
    active for this system/ROM?" without depending on the concrete
    adapter. Implementations own the underlying file reads and may
    cache parse results; ``reset_cache`` lets writers invalidate the
    cache after editing the underlying configuration.
    """

    def get_active_core(
        self,
        system_name: str,
        rom_filename: str | None = None,
    ) -> tuple[str | None, str | None]: ...

    def get_available_cores(self, system_name: str) -> list[dict]: ...

    def reset_cache(self) -> None: ...


class GamelistXmlEditorProtocol(Protocol):
    """Write seam for ES-DE per-system / per-game core overrides.

    Lets ``main.py`` callables mutate ``gamelist.xml`` without
    depending on the concrete adapter. Reads remain a
    ``CoreInfoProvider`` concern.
    """

    def set_system_override(
        self,
        retrodeck_home: str,
        system_name: str,
        core_label: str | None,
    ) -> bool: ...

    def set_game_override(
        self,
        retrodeck_home: str,
        system_name: str,
        rom_path: str,
        core_label: str | None,
    ) -> bool: ...


class CoreNameProviderFn(Protocol):
    """Return the RetroArch canonical ``corename`` for a core shared object.

    Implemented by :class:`adapters.retroarch_core_info.RetroArchCoreInfoAdapter`.
    ``core_so`` is the full ``.so`` basename including the ``_libretro``
    suffix (e.g. ``"snes9x_libretro"``). Returns ``None`` when the ``.info``
    file is missing or lacks a ``corename`` field — callers must fail loud,
    not fall back to ES-DE labels.
    """

    def __call__(self, core_so: str) -> str | None: ...


class RetroArchConfigReader(Protocol):
    """Object seam for ``retroarch.cfg`` reads.

    Held by ``main.py`` to bind ``get_retroarch_save_sorting`` as a
    callable forwarded into service wiring. Distinct from
    :class:`RetroArchSaveSortingProvider` (the call-shaped Protocol for
    the bound method itself) — that one is what services receive; this
    one is what ``main.py`` holds.
    """

    def get_retroarch_save_sorting(self) -> tuple[bool, bool]: ...


class RetroArchCoreInfoReader(Protocol):
    """Object seam for RetroArch per-core ``.info`` reads.

    Held by ``main.py`` to bind ``get_corename`` as a callable
    forwarded into service wiring. Distinct from
    :class:`CoreNameProviderFn` (the call-shaped Protocol for the
    bound method itself) — that one is what services receive; this
    one is what ``main.py`` holds.
    """

    def get_corename(self, core_so: str) -> str | None: ...
