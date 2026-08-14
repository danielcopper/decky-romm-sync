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
    from domain.shortcut_data import EmulatorInvocation
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

    def states_path(self) -> str: ...

    def roms_path(self) -> str: ...

    def bios_path(self) -> str: ...

    def retrodeck_home(self) -> str: ...

    def config_path(self) -> str: ...

    def config_health(self) -> RetroDeckConfigHealth: ...


class RetroArchSaveLayoutProvider(Protocol):
    """Return the live RetroArch save-file layout as a ``SaveLayout`` value object."""

    def __call__(self) -> SaveLayout: ...


class RetroArchSavestateLayoutProvider(Protocol):
    """Return the live RetroArch save**state** layout as a ``SaveLayout`` value object.

    A separate seam from :class:`RetroArchSaveLayoutProvider` because RetroArch
    sorts the two independently — a stock RetroDECK install content-sorts its
    savefiles and leaves its savestates unsorted — so a consumer that needs to
    address savestates must ask about savestates.
    """

    def __call__(self) -> SaveLayout: ...


class CoreResolverFn(Protocol):
    """Resolve the active RetroArch core for a system."""

    def __call__(self, system_name: str) -> tuple[str | None, str | None]: ...


class CoreInfoProvider(Protocol):
    """Emulator resolution for ES-DE configured systems, consumed by services.

    Exposes the read seam services need to answer, from the live
    ``es_systems.xml`` alone, "which emulator is the system-layer default
    for this system, and what else could it launch with?" without depending
    on the concrete adapter. Resolution is system-layer only; the plugin-owned
    per-platform and per-game selections are layered on top by
    ``active_emulator_for_rom``, not here. Implementations own the underlying
    file reads and may cache parse results; ``reset_cache`` lets writers
    invalidate the cache after a per-platform core write.

    ``get_active_core`` stays libretro-only — it feeds the firmware layer's
    system-level BIOS filter, which keys on a RetroArch core. The launch-layer
    default (``get_default_emulator``) and the full picker
    (``get_emulator_options``) are emulator-kind-aware (libretro OR standalone).
    """

    def get_active_core(self, system_name: str) -> tuple[str | None, str | None]: ...

    def get_default_emulator(self, system_name: str) -> EmulatorInvocation | None: ...

    def get_emulator_options(self, system_name: str) -> dict[str, Any]: ...

    def resolve_sandbox_launcher(self, command: str) -> str | None: ...

    def reset_cache(self) -> None: ...


class SystemM3uSupportFn(Protocol):
    """Return whether ES-DE lists ``.m3u`` as a supported extension for a system.

    Backed by ES-DE's own per-system ``<extension>`` list in ``es_systems.xml``
    — the same file ES-DE consults to decide directory-collapse — so a service
    can gate ``.m3u`` generation and launch-file selection on whether the
    platform's emulator can actually read a playlist. Default-safe: ``False``
    for an unknown system or when ``es_systems.xml`` cannot be found.
    """

    def __call__(self, system_name: str) -> bool: ...


class SystemSupportedExtensionsFn(Protocol):
    """Return the extensions ES-DE accepts for a system (lowercased frozenset).

    Backed by the same per-system ``<extension>`` list in ``es_systems.xml`` as
    :class:`SystemM3uSupportFn`, so a service can intersect the live accept-list
    with the disc-image set and never offer a disc the emulator cannot launch,
    and a completed download can be checked for whether the system can launch it
    at all before a launch command is baked.
    Default-safe: an empty frozenset for an unknown system or when
    ``es_systems.xml`` cannot be found (every caller treats the empty answer as
    "cannot tell" and falls back to its permissive branch, never to a refusal).
    """

    def __call__(self, system_name: str) -> frozenset[str]: ...


class SystemKnownFn(Protocol):
    """Whether ``es_systems.xml`` lists a system at all — the same source as the accept-list.

    ``True`` when the system is listed, ``False`` when the file was read and does
    not name it, and ``None`` when the file could not be read, which is a
    different thing from a denial and must not be treated as one.

    The candidate search asks before it searches a platform directory: the
    game-detail page resolves that directory from a RomM slug alone, and an
    unmapped slug is taken verbatim as a directory name. A directory that is not
    an ES-DE system is not a place a game can live, so a namesake inside it is
    content the emulator will never look at. The same answer also protects the
    accept-list's default-safe branch, which reads an empty extension set as
    "cannot tell" and would otherwise let every entry through for a directory
    that was never a system.
    """

    def __call__(self, system_name: str) -> bool | None: ...


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

    Held by ``main.py`` to bind the layout getters as callables
    forwarded into service wiring. Distinct from
    :class:`RetroArchSaveLayoutProvider` / :class:`RetroArchSavestateLayoutProvider`
    (the call-shaped Protocols for the bound methods themselves) — those are what
    services receive; this one is what ``main.py`` holds.
    """

    def get_save_layout(self) -> SaveLayout: ...

    def get_savestate_layout(self) -> SaveLayout: ...


class RetroArchCoreInfoReader(Protocol):
    """Object seam for RetroArch per-core ``.info`` reads.

    Held by ``main.py`` to bind ``get_corename`` as a callable
    forwarded into service wiring. Distinct from
    :class:`CoreNameProviderFn` (the call-shaped Protocol for the
    bound method itself) — that one is what services receive; this
    one is what ``main.py`` holds.
    """

    def get_corename(self, core_so: str) -> str | None: ...
