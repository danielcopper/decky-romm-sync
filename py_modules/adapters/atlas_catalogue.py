"""Atlas catalogue adapter — the seam through which emulator questions reach the resolver.

The single place the vendored `emu-atlas <https://github.com/danielcopper/emu-atlas>`_
resolver is asked what a frontend's catalogue declares for a system: the emulator
list behind the picker, the system-layer default the launch bakes, the libretro
core the firmware filter keys on, and the accept-list a candidate file is matched
against. Services see :class:`domain.emulator_commands.EmulatorOption` values and
plain sets and never an atlas type — ``domain/`` may not import ``_vendor`` at
all (the ``domain-stdlib-only`` contract), so the vocabulary and the resolver
have to meet at an adapter.

Three properties of the resolver decide this module's shape.

**It answers per installation, and picking one is not its job.** ``detect``
returns every arrangement it found and never a winner, so the choice arrives
here as an injected callable. Nothing in ``services/`` learns which arrangement
answered; offering more than one is #918's, and until then the wiring hands over
"the first detected", which is atlas's own order with RetroDECK at its head.

**Its entry order is the EFFECTIVE one, and this plugin wants the declared one.**
A gamelist ``<altemulator>`` or a system-level ``<alternativeEmulator>`` promotes
an entry to the front of the answer, and the plugin keeps the gamelist off every
launch path (ADR-0012). The shipped position survives promotion as
``declared_index``, so ordering by it recovers exactly the order ES-DE's own file
declares — which is what ADR-0020's "first safely-bakeable command in document
order" is a rule about. Never ``entries[0]``, and never an assumption that index
0 exists: upstream mirrors ES-DE's own walk, where an empty-text ``<command>``
holds a position without yielding an entry and a duplicate label takes none.

**It never logs, and it raises on its own invariant violations** rather than
returning a degraded answer. Caveats are its whole degradation channel, and their
``code`` is the stable half of that contract while ``message`` is prose that may
change freely: the codes reaching the log go through the injected debug logger,
and nothing here parses a message. Every resolver call is wrapped, and a failure
becomes the same answer an unreadable catalogue gives — "unavailable", never an
empty list a caller would read as "this frontend knows no emulator".

An empty entry list is five different facts and the codes tell them apart, so
:data:`_CATALOGUE_REFUSALS` is what decides "unavailable" — never an empty
``caveats``, which a healthy answer does not have either (a broken installation
states its health findings on every answer it gives).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from _vendor.atlas import (
    CAVEAT_EMULATOR_CATALOGUE_SEALED,
    CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE,
    CAVEAT_EMULATOR_CATALOGUE_UNESTABLISHED,
    CAVEAT_EMULATOR_CATALOGUE_UNREADABLE,
    KIND_LIBRETRO,
    detect,
)

from domain.emulator_commands import (
    EmulatorOption,
    classify_command,
    downgrade_if_not_installed,
    option_to_invocation,
    select_default_option,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from domain.shortcut_data import EmulatorInvocation

# The four codes that mean nobody could answer from a catalogue at all: the
# arrangement ships none, atlas has not established where it keeps one, the one
# it has could not be read, or part of it sits where atlas does not open. Only
# their absence makes an empty entry list a statement about the machine — "the
# catalogue was read and this frontend knows no emulator for this system" — and
# that is the one empty the picker may render as an empty list.
# ``emulator-catalogue-exclusive`` is deliberately not here: it says a custom
# es_systems.xml declared itself the whole catalogue, so the answer is COMPLETE
# and merely small.
_CATALOGUE_REFUSALS = frozenset(
    {
        CAVEAT_EMULATOR_CATALOGUE_UNAVAILABLE,
        CAVEAT_EMULATOR_CATALOGUE_UNESTABLISHED,
        CAVEAT_EMULATOR_CATALOGUE_UNREADABLE,
        CAVEAT_EMULATOR_CATALOGUE_SEALED,
    }
)

_CORE_SO_SUFFIX = ".so"

_UNAVAILABLE: dict[str, Any] = {"available": False, "options": []}


def first_detected_installation(user_home: str) -> Any:
    """The highest-priority arrangement detected under *user_home*, or ``None``.

    Detection returns what it found in probe order — RetroDECK, EmuDeck, an
    unclaimed bare RetroArch flatpak, a bare native RetroArch — and never picks a
    winner. This plugin is a RetroDECK plugin, and RetroDECK leads that order
    where it is present, so "the first" is the RetroDECK answer wherever there is
    one. Offering the others is #918.
    """
    installations = detect(user_home)
    return installations[0] if installations else None


def _plugin_core_so(core_so: str) -> str:
    """Atlas's ``mgba_libretro.so`` in the plugin's own identifier space.

    Every core identifier the plugin holds — the resolved active core, the
    ``platform_cores`` override, an ES-DE ``<command>``'s core — is the bare
    ``.so`` basename without its extension. Comparing the two spellings without
    normalising here would silently match nothing.
    """
    return core_so[: -len(_CORE_SO_SUFFIX)] if core_so.endswith(_CORE_SO_SUFFIX) else core_so


def _caveat_codes(answer: Any) -> tuple[str, ...]:
    """Every stable caveat code the answer states."""
    return tuple(caveat.code for caveat in answer.caveats)


def _refused(answer: Any) -> bool:
    """Whether the answer says nobody could read a catalogue for this system."""
    return not _CATALOGUE_REFUSALS.isdisjoint(_caveat_codes(answer))


def _declared_order(entries: tuple[Any, ...]) -> list[Any]:
    """The answer's entries in the order the declaring layer ships them.

    The answer arrives in effective order, where a user's promotion may put a
    higher declared position first; ``declared_index`` is the shipped one, which
    promotion never touches. Sorting on it is what turns the effective order back
    into the declared one that ADR-0020's default rule is written about.

    An entry with no declared position sorts last, in the order the answer gave
    it. That is the derived enumeration (``emulator-list-derived``): no layer
    declared it, so it has no shipped place to sort by. Such an entry cannot
    become the default even from the back of the list, because a derived entry
    carries an EMPTY command, which the classifier reads as ``no_rom_target``.
    """
    return sorted(
        entries,
        key=lambda entry: (entry.declared_index is None, entry.declared_index or 0),
    )


class AtlasCatalogueAdapter:
    """Resolves the emulators a frontend catalogue declares, live, through the vendored resolver.

    Implements the ``CoreInfoProvider`` Protocol structurally, plus the three
    call-shaped system questions (:class:`services.protocols.SystemM3uSupportFn`,
    ``SystemSupportedExtensionsFn``, ``SystemKnownFn``) as bound methods.

    Resolution is system-layer only; the plugin-owned per-platform and per-game
    selections are layered on top by
    :class:`services.active_core_resolver.ActiveCoreResolver`, not here — and
    ES-DE's own selections are ignored, which is what ``_declared_order`` is for.

    Caches the chosen installation and every answer read through it as instance
    attributes. There is no mtime guard to fall back on any more: what the
    resolver read to answer is its own business, so a change to ES-DE's catalogue
    lands on a :meth:`reset_cache` (which a per-platform core write already
    performs) or on the next plugin reload.
    """

    def __init__(
        self,
        *,
        choose_installation: Callable[[], Any],
        emulator_installed: Callable[[str], bool],
        log_debug: Callable[[str], None],
    ) -> None:
        self._choose_installation = choose_installation
        self._emulator_installed = emulator_installed
        self._log_debug = log_debug
        self._installation: Any = None
        self._catalogues: dict[str, Any] = {}
        self._locations: dict[str, Any] = {}
        self._systems: Any = None

    def reset_cache(self) -> None:
        """Drop the chosen installation and every answer read through it.

        Call after a per-platform core write so the next resolution re-reads from
        disk instead of returning a stale answer. Dropping the installation with
        the answers is what makes the cache one generation rather than two: an
        answer only ever describes the arrangement it was read from.
        """
        self._installation = None
        self._catalogues = {}
        self._locations = {}
        self._systems = None

    # -- public API ----------------------------------------------------------

    def get_active_core(self, system_name: str) -> tuple[str | None, str | None]:
        """Resolve the system-layer libretro active core for a system.

        The first libretro entry in declared order, as ``(core_so, label)``.
        Libretro-only by design — this feeds the firmware layer's system-level
        BIOS filter, which keys on a RetroArch core, not the launch-layer default
        (which may be a standalone emulator). ``(None, None)`` when the system
        offers no libretro entry, when the catalogue could not be read, or when
        no installation was detected.

        Reads the resolver's own ``kind``, not the bake classifier's: whether a
        command loads a core is a fact about the command, where bakeability is a
        fact about this plugin's ``-e`` override. A libretro command the plugin
        cannot bake still names the core the BIOS filter is about.
        """
        answer = self._catalogue_answer(system_name)
        if answer is None or _refused(answer):
            return (None, None)
        for entry in _declared_order(answer.entries):
            if entry.kind == KIND_LIBRETRO and entry.core_so:
                return (_plugin_core_so(entry.core_so), entry.label)
        return (None, None)

    def get_default_emulator(self, system_name: str) -> EmulatorInvocation | None:
        """Resolve the system-layer default **emulator** (libretro OR standalone).

        The first *safely-bakeable* entry in declared order
        (:func:`domain.emulator_commands.select_default_option`) rendered into an
        :class:`EmulatorInvocation` — a libretro core or a standalone emulator,
        whichever the catalogue declares first that the plugin can bake. Returns
        ``None`` when nothing is bakeable (or the catalogue could not be read);
        the caller bakes the plain RetroDECK launch and lets RetroDECK resolve the
        emulator itself.

        Keeps the read-path/launch-path invariant: the resolved emulator is both
        what the ROM launches with and what derived values key on.
        """
        result = self.get_emulator_options(system_name)
        if not result["available"]:
            return None
        return option_to_invocation(select_default_option(result["options"]))

    def get_emulator_options(self, system_name: str) -> dict[str, Any]:
        """Return every catalogue entry for a system, classified for bakeability.

        Returns ``{"available": bool, "options": [EmulatorOption, ...]}``.
        ``available`` is ``False`` when the answer carries one of the four
        catalogue refusals, or when no installation was detected at all — the
        caller surfaces that as "emulator list unavailable" rather than seeing an
        empty list it cannot distinguish from a system the frontend knows no
        emulator for. ``options`` is in DECLARED order, so the first bakeable
        entry is the system default. A bakeable **standalone** option whose
        emulator is not installed in RetroDECK is downgraded to ``needs_setup``
        (reason ``not_installed``) via the injected find-rules probe, so it
        neither becomes the default nor bakes into a shortcut. A system the
        catalogue does not declare yields ``available: True`` with an empty list.
        """
        answer = self._catalogue_answer(system_name)
        if answer is None or _refused(answer):
            return dict(_UNAVAILABLE)
        options = [
            self._probe_installed(classify_command(entry.label, entry.command))
            for entry in _declared_order(answer.entries)
        ]
        return {"available": True, "options": options}

    def system_supports_m3u(self, system_name: str) -> bool:
        """True iff the catalogue lists ``.m3u`` as a supported extension for *system_name*.

        Reads the same per-system accept-list ES-DE uses to decide
        directory-collapse, so the answer can never disagree with ES-DE. Returns
        ``False`` when the system is unknown or the catalogue could not be read
        (default-safe: a missing playlist only degrades; a wrong one breaks the
        launch).
        """
        return ".m3u" in self.get_supported_extensions(system_name)

    def is_known_system(self, system_name: str) -> bool | None:
        """Whether the frontend's catalogue declares *system_name*.

        ``None`` when the catalogue could not be read at all — the caller must not
        read a source that did not answer as a denial, which is the same
        default-safe rule :meth:`get_supported_extensions` follows by returning an
        empty set. ``False`` is therefore a positive statement: this catalogue was
        read, and it does not name this system.
        """
        answer = self._systems_answer()
        if answer is None or _refused(answer):
            return None
        return system_name in answer.systems

    def get_supported_extensions(self, system_name: str) -> frozenset[str]:
        """Return the extensions the catalogue accepts for *system_name* (lowercased).

        Reads the same per-system declaration ES-DE consults, so a caller can
        intersect it with the disc-image set and never offer a disc the emulator
        cannot launch. The tokens arrive verbatim, both cases listed where the
        file lists both, so they are lowercased here to make membership checks
        case-insensitive. Returns an empty frozenset for an unknown system or when
        the catalogue could not be read (default-safe: the caller falls back to
        the full disc set).
        """
        placement = self._rom_location(system_name)
        if placement is None:
            return frozenset()
        return frozenset(token.lower() for token in placement.extensions)

    # -- helpers -------------------------------------------------------------

    def _probe_installed(self, option: EmulatorOption) -> EmulatorOption:
        """Downgrade a bakeable standalone whose emulator is not installed.

        Libretro options and already-non-bakeable options pass through untouched
        (RetroArch ships with RetroDECK; the domain rule guards the kind). For a
        bakeable standalone, hand the find-rules probe's on-disk verdict to
        :func:`domain.emulator_commands.downgrade_if_not_installed`.
        """
        if option.status != "bakeable" or option.kind != "standalone":
            return option
        return downgrade_if_not_installed(option, self._emulator_installed(option.command))

    def _ask(self, question: Callable[[], Any], subject: str) -> Any:
        """Put one question to the resolver, or answer ``None`` where it could not be asked.

        Deliberately broad: the resolver's failure modes are its own invariant
        assertions and its packaged-data loaders, neither of which is an
        exception type this adapter should enumerate. The honest answer to "we
        could not ask" is the same whatever raised, and every caller of this
        already has a ``None`` branch — the one an unreadable catalogue takes.
        """
        try:
            return question()
        except Exception as exc:
            self._log_debug(f"[catalogue] resolver failed on {subject}: {exc!r}")
            return None

    def _installation_handle(self) -> Any:
        """The chosen installation, memoised, or ``None`` when nothing was detected.

        A detection that found nothing is deliberately NOT memoised: a RetroDECK
        installed while the plugin is running is then picked up on the next call,
        which is what the parser's every-call flatpak probe gave for free. The
        cost is one detection per call in the one state where nothing resolves
        anyway.
        """
        if self._installation is None:
            self._installation = self._ask(self._choose_installation, "detection")
            if self._installation is None:
                self._log_debug("[catalogue] no emulator installation detected")
        return self._installation

    def _catalogue_answer(self, system_name: str) -> Any:
        """The catalogue's answer for *system_name*, cached, or ``None`` with nothing to ask.

        Asked without a content path: a per-game ``<altemulator>`` would promote
        an entry the plugin ignores anyway (ADR-0012), and the answer is the
        system's rather than one game's.
        """
        installation = self._installation_handle()
        if installation is None:
            return None
        if system_name not in self._catalogues:
            answer = self._ask(lambda: installation.emulators_for(system_name), f"emulators_for({system_name!r})")
            if answer is None:
                return None
            self._log_debug(
                f"[catalogue] {system_name}: entries={len(answer.entries)} caveats={sorted(set(_caveat_codes(answer)))}"
            )
            self._catalogues[system_name] = answer
        return self._catalogues[system_name]

    def _systems_answer(self) -> Any:
        """Every system the catalogue declares, cached, or ``None`` with nothing to ask.

        Cached like the per-system answers rather than asked per call: this one
        is per installation, it enumerates every system the catalogue has, and
        the caller asks it once per platform a candidate search visits.
        """
        installation = self._installation_handle()
        if installation is None:
            return None
        if self._systems is None:
            self._systems = self._ask(lambda: installation.systems(), "systems")
        return self._systems

    def _rom_location(self, system_name: str) -> Any:
        """Where *system_name*'s content lives and what it accepts, cached, or ``None``."""
        installation = self._installation_handle()
        if installation is None:
            return None
        if system_name not in self._locations:
            placement = self._ask(lambda: installation.rom_location(system_name), f"rom_location({system_name!r})")
            if placement is None:
                return None
            self._locations[system_name] = placement
        return self._locations[system_name]
