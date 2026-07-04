"""In-memory ``CoreInfoProvider`` implementation for service tests."""

from __future__ import annotations

from typing import Any

from domain.emulator_commands import EmulatorOption
from domain.shortcut_data import EmulatorInvocation


def libretro_option(core_so: str, label: str) -> EmulatorOption:
    """Build a bakeable libretro :class:`EmulatorOption` for a core.

    Mirrors the real classifier's output for the RetroArch ``-L`` shape, so a
    test can seed the fake with ``options=[libretro_option("mgba_libretro",
    "mGBA"), ...]`` and have ``label_to_invocation`` resolve the label to the
    same libretro invocation the adapter would.
    """
    return EmulatorOption(
        label=label,
        kind="libretro",
        core_so=core_so,
        command=f"%EMULATOR_RETROARCH% -L %CORE_RETROARCH%/{core_so}.so %ROM%",
        status="bakeable",
        reason=None,
    )


def standalone_option(
    command: str, label: str, *, status: str = "bakeable", reason: str | None = None
) -> EmulatorOption:
    """Build a standalone :class:`EmulatorOption` from a full ES-DE command.

    Defaults to a bakeable option; pass ``status``/``reason`` to seed a
    ``needs_setup`` or ``unbakeable`` entry (e.g. to prove a per-game pin to an
    un-bakeable label hard-fails).
    """
    return EmulatorOption(label=label, kind="standalone", core_so=None, command=command, status=status, reason=reason)


class FakeCoreInfoProvider:
    """In-memory CoreInfoProvider for tests.

    Returns the configured ``active_core`` (the libretro system default, for the
    firmware BIOS filter) and ``options`` (the classified emulator picker list)
    for any system. ``options`` may be seeded directly with
    :class:`EmulatorOption` values or synthesized from the ``available_cores``
    convenience — a list of ``{"core_so", "label", "is_default"}`` dicts turned
    into bakeable libretro options (the shape most core-selection tests use).
    ``available`` mirrors the adapter's "es_systems.xml readable?" flag.

    ``reset_cache`` increments ``reset_cache_count`` so writers can assert the
    cache was invalidated after a write. ``active_core_calls`` and
    ``emulator_options_calls`` record the ``system_name`` each seam was invoked
    with so callers can assert a normalized system (not the raw platform slug)
    reached the read seam.

    ``standalone`` maps a system name to a standalone-emulator
    :class:`EmulatorInvocation`. When a system carries one,
    :meth:`get_default_emulator` returns it directly; otherwise it mirrors the
    real adapter and projects :meth:`get_active_core` into a libretro invocation
    (so ``get_active_core`` is still consulted — and recorded — for the libretro
    path).
    """

    def __init__(
        self,
        *,
        active_core: tuple[str | None, str | None] = (None, None),
        available_cores: list[dict[str, Any]] | None = None,
        options: list[EmulatorOption] | None = None,
        available: bool = True,
        standalone: dict[str, EmulatorInvocation] | None = None,
        sandbox_launchers: dict[str, str] | None = None,
    ) -> None:
        self.active_core = active_core
        self._available_cores: list[dict[str, Any]] = []
        if options is not None:
            self.options: list[EmulatorOption] = options
        else:
            # Route through the setter so ``options`` is synthesized once and
            # stays in sync with any later ``fake.available_cores = [...]`` write.
            self.available_cores = available_cores or []
        self.available = available
        self.standalone: dict[str, EmulatorInvocation] = standalone if standalone is not None else {}
        # Maps a standalone command → its sandbox launcher path (the folder-boot
        # direct-bake probe); an unseeded command resolves to ``None``.
        self.sandbox_launchers: dict[str, str] = sandbox_launchers if sandbox_launchers is not None else {}
        self.reset_cache_count = 0
        self.active_core_calls: list[str] = []
        self.emulator_options_calls: list[str] = []
        self.sandbox_launcher_calls: list[str] = []

    @property
    def available_cores(self) -> list[dict[str, Any]]:
        """The libretro-core convenience input, kept in sync with ``options``."""
        return self._available_cores

    @available_cores.setter
    def available_cores(self, value: list[dict[str, Any]]) -> None:
        self._available_cores = value
        self.options = [libretro_option(c["core_so"], c["label"]) for c in value if "core_so" in c]

    def get_active_core(self, system_name: str) -> tuple[str | None, str | None]:
        self.active_core_calls.append(system_name)
        return self.active_core

    def get_default_emulator(self, system_name: str) -> EmulatorInvocation | None:
        pref = self.standalone.get(system_name)
        if pref is not None:
            return pref
        core_so, label = self.get_active_core(system_name)
        if core_so:
            return EmulatorInvocation.libretro(core_so, label)
        return None

    def get_emulator_options(self, system_name: str) -> dict[str, Any]:
        self.emulator_options_calls.append(system_name)
        return {"available": self.available, "options": self.options}

    def resolve_sandbox_launcher(self, command: str) -> str | None:
        self.sandbox_launcher_calls.append(command)
        return self.sandbox_launchers.get(command)

    def reset_cache(self) -> None:
        self.reset_cache_count += 1
