"""ActiveCoreResolver — the single read-path core-resolution seam per ROM.

The one place that answers "which RetroArch core will this ROM actually launch
with?", combining the per-game ``emulator_override`` and per-platform core
selection (the two deviations the plugin owns) with the system-layer
ES-DE/RetroDECK resolution. Every per-game core read consumer and every
launch-bake site draws from this seam so the read-path core never diverges from
the launched core.

Precedence (three layers, then the plain launch): DB ``emulator_override`` (top)
→ ``settings.json`` per-platform core → the live es_systems default → ``None``.
The retired ES-DE gamelist ``<alternativeEmulator>`` is never consulted, and
there is no offline snapshot below the live default. A pinned per-game or
per-platform label that no longer resolves to a bakeable emulator degrades to
the next layer rather than raising — so a stale label never blocks a read or
bakes a bogus ``-e`` override. The per-game/per-platform label may name a
**standalone** emulator (not just a libretro core); it resolves the same way.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from domain.emulator_commands import label_to_invocation

if TYPE_CHECKING:
    import logging

    from domain.rom import Rom
    from domain.shortcut_data import EmulatorInvocation
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
    (the classified emulator options + the system-layer default), the
    per-platform core reader (the ``settings.json`` ``platform_cores`` map), the
    platform-slug-to-system resolver, and the logger used to warn on a stale
    label.
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

    def active_emulator_for_rom(self, rom_id: int) -> EmulatorInvocation | None:
        """Return the :class:`EmulatorInvocation` the ROM ``rom_id`` will launch with.

        The launch-bake seam. Reads the ROM's ``platform_slug`` +
        ``emulator_override`` once, then applies the three-layer precedence:

        1. Per-game DB ``emulator_override`` (an emulator LABEL from the picker,
           libretro OR standalone) → its invocation when the label resolves to a
           bakeable command.
        2. Per-platform ``settings.json`` core (also a LABEL, libretro OR
           standalone) → its invocation when it resolves.
        3. The live es_systems default via ``get_default_emulator`` — the first
           safely-bakeable command, which may itself be standalone (PCSX2, RPCS3,
           Dolphin, …) or libretro.

        Returns ``None`` when the platform has no resolvable emulator at all —
        including when ``es_systems.xml`` cannot be read — so the caller bakes
        the plain launch and lets RetroDECK resolve the emulator. A stale
        per-game/per-platform label is never fatal — it degrades to the next
        layer with a WARNING.
        """
        rom = self._read_rom(rom_id)
        if rom is None:
            self._logger.warning("active_core_resolver: no ROM for rom_id=%s; resolving to plain launch", rom_id)
            return None

        system = self._resolve_system(rom.platform_slug)
        options = self._core_info.get_emulator_options(system)["options"]

        override = rom.emulator_override
        if override is not None:
            invocation = label_to_invocation(options, override)
            if invocation is not None:
                return invocation
            self._logger.warning(
                "active_core_resolver: per-game override '%s' for rom_id=%s no longer resolves on %s; "
                "degrading to the per-platform/system default",
                override,
                rom_id,
                system,
            )

        platform_label = self._platform_core_reader.get_platform_core(rom.platform_slug)
        if platform_label is not None:
            invocation = label_to_invocation(options, platform_label)
            if invocation is not None:
                return invocation
            self._logger.warning(
                "active_core_resolver: per-platform core '%s' for %s (rom_id=%s) no longer resolves; "
                "degrading to the system default",
                platform_label,
                rom.platform_slug,
                rom_id,
            )

        return self._core_info.get_default_emulator(system)

    def active_core_for_rom(self, rom_id: int) -> tuple[str | None, str | None]:
        """Return the ``(core_so, label)`` the ROM ``rom_id`` will launch with.

        The read-path projection of :meth:`active_emulator_for_rom`, kept for the
        ``.so``-space consumers (BIOS status, per-core save dir, save-emulator
        tag, core-change detection, the cores menu's active marker). A **libretro**
        emulator yields its ``(core_so, label)``; a **standalone** emulator yields
        ``(None, label)`` — those consumers already degrade on a ``None`` core
        exactly as they did for the old ``(None, None)`` resolution, so the
        read-path core never disagrees with the (now possibly standalone) launch.
        """
        emulator = self.active_emulator_for_rom(rom_id)
        if emulator is None:
            return (None, None)
        return (emulator.core_so, emulator.label)

    def _read_rom(self, rom_id: int) -> Rom | None:
        with self._uow_factory() as uow:
            return uow.roms.get(rom_id)
