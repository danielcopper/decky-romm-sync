"""RetroDECK runtime path, system, and core resolution Protocols.

Services query the host RetroDECK/RetroArch/ES-DE environment through
these Protocols: filesystem path getters (saves, roms, BIOS,
RetroDECK home), platform-to-system resolution, the RetroArch save-file
layout, and RetroArch core lookups for ES-DE configured systems.
``PlatformCoreReader`` exposes the plugin-owned per-platform core
selection (stored in ``settings.json``, not the ES-DE gamelist) that the
resolver layers over the es_systems default.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from domain.save_layout import SaveLayout
    from lib.retrodeck_health import RetroDeckConfigHealth


class SystemResolver(Protocol):
    """Resolve a RomM platform slug to a RetroDECK system path."""

    def __call__(self, platform_slug: str, platform_fs_slug: str | None = None) -> str: ...


class RetroDeckPaths(Protocol):
    """Bundled accessor for the RetroDECK runtime directory paths plus a
    health signal for how trustworthy those paths are.

    Distinct method names per path are deliberate: a single
    ``def __call__(self) -> str`` shape would make a saves-for-bios
    mix-up silently type-check at the call site. Separate names give
    the type checker enough information to flag it. The path getters are
    best-effort and never raise; ``config_health`` is the loud signal
    ``main.py`` surfaces to the frontend when the resolved roots are
    likely wrong (``retrodeck.json`` unreadable, or its resolved home
    missing on disk).
    """

    def saves_path(self) -> str: ...

    def roms_path(self) -> str: ...

    def bios_path(self) -> str: ...

    def retrodeck_home(self) -> str: ...

    def config_path(self) -> str: ...

    def config_health(self) -> RetroDeckConfigHealth: ...


class RetroArchSaveLayoutProvider(Protocol):
    """Return the live RetroArch save-file layout as a ``SaveLayout`` value object."""

    def __call__(self) -> SaveLayout: ...


class CoreResolverFn(Protocol):
    """Resolve the active RetroArch core for a system."""

    def __call__(self, system_name: str) -> tuple[str | None, str | None]: ...


class CoreInfoProvider(Protocol):
    """Core resolution for ES-DE configured systems, consumed by services.

    Exposes the read seam services need to ask "which RetroArch core is
    the system-layer default for this system?" without depending on the
    concrete adapter. Resolution is system-layer only (es_systems default
    → ``core_defaults``); the plugin-owned per-platform and per-game core
    selections are layered on top by ``active_core_for_rom``, not here.
    Implementations own the underlying file reads and may cache parse
    results; ``reset_cache`` lets writers invalidate the cache after a
    per-platform core write.
    """

    def get_active_core(self, system_name: str) -> tuple[str | None, str | None]: ...

    def get_available_cores(self, system_name: str) -> list[dict[str, Any]]: ...

    def reset_cache(self) -> None: ...


class PlatformCoreReader(Protocol):
    """Read seam for the plugin-owned per-platform core selection.

    Exposes the ``settings.json`` ``platform_cores`` map (RomM platform
    slug → core label) so the resolver can layer a user-chosen
    platform-wide core over the es_systems default without reading the
    retired ES-DE gamelist. Returns the stored core label for a slug, or
    ``None`` when the platform has no plugin-owned selection.
    """

    def get_platform_core(self, platform_slug: str) -> str | None: ...


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

    Held by ``main.py`` to bind ``get_save_layout`` as a callable
    forwarded into service wiring. Distinct from
    :class:`RetroArchSaveLayoutProvider` (the call-shaped Protocol for
    the bound method itself) — that one is what services receive; this
    one is what ``main.py`` holds.
    """

    def get_save_layout(self) -> SaveLayout: ...


class RetroArchCoreInfoReader(Protocol):
    """Object seam for RetroArch per-core ``.info`` reads.

    Held by ``main.py`` to bind ``get_corename`` as a callable
    forwarded into service wiring. Distinct from
    :class:`CoreNameProviderFn` (the call-shaped Protocol for the
    bound method itself) — that one is what services receive; this
    one is what ``main.py`` holds.
    """

    def get_corename(self, core_so: str) -> str | None: ...
