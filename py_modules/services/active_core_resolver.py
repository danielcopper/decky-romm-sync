"""ActiveCoreResolver — the single read-path core-resolution seam per ROM.

The one place that answers "which RetroArch core will this ROM actually launch
with?", combining the per-game ``emulator_override`` and per-platform core
selection (the two deviations the plugin owns) with the system-layer
ES-DE/RetroDECK resolution. Every per-game core read consumer and every
launch-bake site draws from this seam so the read-path core never diverges from
the launched core.

Precedence: DB ``emulator_override`` (top) → ``settings.json`` per-platform core
→ es_systems default → core_defaults. The retired ES-DE gamelist
``<alternativeEmulator>`` is never consulted. A pinned per-game or per-platform
label that no longer resolves to a ``.so`` degrades to the next layer rather than
raising — so a stale label never blocks a read or bakes a bogus ``None.so``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.shortcut_data import label_to_core_so

if TYPE_CHECKING:
    import logging

    from domain.rom import Rom
    from services.protocols import (
        CoreInfoProvider,
        PlatformCoreReader,
        SystemResolver,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class ActiveCoreResolverConfig:
    """Frozen wiring bundle handed to ``ActiveCoreResolver.__init__``.

    Carries the SQLite Unit-of-Work factory (to read the ROM's
    ``platform_slug`` + ``emulator_override``), the ES-DE core-info read seam
    (available cores + system-layer active core), the per-platform core reader
    (the ``settings.json`` ``platform_cores`` map), the platform-slug-to-system
    resolver, and the logger used to warn on a stale label.
    """

    uow_factory: UnitOfWorkFactory
    core_info: CoreInfoProvider
    platform_core_reader: PlatformCoreReader
    resolve_system: SystemResolver
    logger: logging.Logger


class ActiveCoreResolver:
    """Resolve the active RetroArch core for one ROM by ``rom_id``."""

    def __init__(self, *, config: ActiveCoreResolverConfig) -> None:
        self._uow_factory = config.uow_factory
        self._core_info = config.core_info
        self._platform_core_reader = config.platform_core_reader
        self._resolve_system = config.resolve_system
        self._logger = config.logger

    def active_core_for_rom(self, rom_id: int) -> tuple[str | None, str | None]:
        """Return the ``(core_so, label)`` the ROM ``rom_id`` will launch with.

        Reads the ROM's ``platform_slug`` + ``emulator_override`` once, then
        applies the four-layer precedence:

        1. Per-game DB ``emulator_override``: resolves the stored LABEL through
           the es_systems ``available_cores`` map and returns ``(core_so,
           label)`` when it resolves.
        2. Per-platform ``settings.json`` core: resolves the platform's stored
           LABEL the same way and returns it when it resolves.
        3. es_systems default (system-layer ``get_active_core``).
        4. ``core_defaults`` fallback (also via ``get_active_core``).

        The system layer may yield ``(None, None)`` when nothing is configured;
        that passes through unchanged. A stale per-game or per-platform label is
        never fatal and never produces a bogus ``.so`` — it degrades to the next
        layer with a WARNING.
        """
        rom = self._read_rom(rom_id)
        if rom is None:
            self._logger.warning("active_core_resolver: no ROM for rom_id=%s; resolving to (None, None)", rom_id)
            return (None, None)

        system = self._resolve_system(rom.platform_slug)
        available = self._core_info.get_available_cores(system)

        override = rom.emulator_override
        if override is not None:
            core_so = label_to_core_so(available, override)
            if core_so is not None:
                return (core_so, override)
            self._logger.warning(
                "active_core_resolver: per-game override '%s' for rom_id=%s no longer resolves on %s; "
                "degrading to the per-platform/system default",
                override,
                rom_id,
                system,
            )

        platform_label = self._platform_core_reader.get_platform_core(rom.platform_slug)
        if platform_label is not None:
            core_so = label_to_core_so(available, platform_label)
            if core_so is not None:
                return (core_so, platform_label)
            self._logger.warning(
                "active_core_resolver: per-platform core '%s' for %s (rom_id=%s) no longer resolves; "
                "degrading to the system default",
                platform_label,
                rom.platform_slug,
                rom_id,
            )

        return self._core_info.get_active_core(system)

    def _read_rom(self, rom_id: int) -> Rom | None:
        with self._uow_factory() as uow:
            return uow.roms.get(rom_id)
