"""ActiveCoreResolver — the single read-path core-resolution seam per ROM.

The one place that answers "which RetroArch core will this ROM actually launch
with?", combining the per-game ``emulator_override`` (the deviation the plugin
owns) with the system-layer ES-DE/RetroDECK resolution (the layers RetroDECK
owns). Every per-game core read consumer and every launch-bake site draws from
this seam so the read-path core never diverges from the launched core.

Precedence: DB ``emulator_override`` (top) → system ``<alternativeEmulator>`` →
es_systems default → core_defaults. A pinned override that no longer resolves to
a ``.so`` degrades to the system-layer result rather than raising — mirroring the
graceful-degrade the bake path applies, so a stale label never blocks a read.
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
        SystemResolver,
        UnitOfWorkFactory,
    )


@dataclass(frozen=True)
class ActiveCoreResolverConfig:
    """Frozen wiring bundle handed to ``ActiveCoreResolver.__init__``.

    Carries the SQLite Unit-of-Work factory (to read the ROM's
    ``platform_slug`` + ``emulator_override``), the ES-DE core-info read seam
    (available cores + system-layer active core), the platform-slug-to-system
    resolver, and the logger used to warn on a stale override.
    """

    uow_factory: UnitOfWorkFactory
    core_info: CoreInfoProvider
    resolve_system: SystemResolver
    logger: logging.Logger


class ActiveCoreResolver:
    """Resolve the active RetroArch core for one ROM by ``rom_id``."""

    def __init__(self, *, config: ActiveCoreResolverConfig) -> None:
        self._uow_factory = config.uow_factory
        self._core_info = config.core_info
        self._resolve_system = config.resolve_system
        self._logger = config.logger

    def active_core_for_rom(self, rom_id: int) -> tuple[str | None, str | None]:
        """Return the ``(core_so, label)`` the ROM ``rom_id`` will launch with.

        Reads the ROM's ``platform_slug`` + ``emulator_override`` once, then:

        1. With a resolvable override: runs the stored LABEL through the
           es_systems ``available_cores`` map and returns ``(core_so, label)``.
        2. With no override (NULL) **or** an override whose label no longer
           resolves (stale): delegates to the system-layer ``get_active_core``
           (system ``<alternativeEmulator>`` → es_systems default →
           core_defaults) and returns its ``(core_so, label)``.

        The system layer may yield ``(None, None)`` when nothing is configured;
        that passes through unchanged. A stale override is never fatal and never
        produces a bogus ``.so`` — it degrades to the system result with a
        WARNING.
        """
        rom = self._read_rom(rom_id)
        if rom is None:
            self._logger.warning("active_core_resolver: no ROM for rom_id=%s; resolving to (None, None)", rom_id)
            return (None, None)

        system = self._resolve_system(rom.platform_slug)
        override = rom.emulator_override
        if override is not None:
            core_so = label_to_core_so(self._core_info.get_available_cores(system), override)
            if core_so is not None:
                return (core_so, override)
            self._logger.warning(
                "active_core_resolver: override '%s' for rom_id=%s no longer resolves on %s; "
                "degrading to the system default",
                override,
                rom_id,
                system,
            )

        return self._core_info.get_active_core(system)

    def _read_rom(self, rom_id: int) -> Rom | None:
        with self._uow_factory() as uow:
            return uow.roms.get(rom_id)
