"""Asking every installation at once — the aggregate over :func:`atlas.detect`.

Choosing an installation is optional here, not abolished. A machine can carry
RetroDECK and a native RetroArch side by side, and "where does this save live?"
then has two true answers — one per arrangement, PPSSPP once with the RetroDECK
wiring and once with the native one. :class:`EveryInstallation` asks all of them
and hands back every answer, each labelled with the handle that produced it.
Returning every true answer is not guessing; picking a winner would be, which is
why nothing here merges, deduplicates or prefers. A one-installation machine
yields a one-entry result, and the caller still never chose.

The aggregate adds nothing to an answer. It delegates every question to the
handles unchanged, in the order :func:`~atlas.detect.detect` returned them
(documented probe order, highest priority first), so a labelled answer is
byte-identical to the one the handle route gives for the same question. Every
resolver rule stays where it is resolved — in the handle — which makes this the
one question-answering surface in atlas that reads nothing off the machine
itself: it holds no seam and performs no read.

Three consequences worth stating, because each is a decision:

- **The label is the handle**, not a copy of its identity. A consumer that wants
  to drill down asks it the next question (``emulators_for``, then the entry's
  own ``savefile_location``), which a detached ``(kind, root)`` pair cannot answer;
  and identity read off a handle is a live read, which a snapshot taken at
  fan-out time would silently age. So ``kind``, ``kinds`` and ``root()`` are
  reached through the label instead of being fanned out. ``health()`` *is*
  fanned out: it is a reading of the installation's state, not a name for it,
  and a caller checks it before trusting the answers beside it.
- **An empty machine answers with nothing, and that is a result.** It is
  :func:`~atlas.detect.detect`'s own empty answer, which has exactly one
  meaning: no marker was found. Detection triggers on marker *existence*, so a
  present-but-broken installation still yields a handle and never hides inside
  this empty — and every other kind of empty is a per-installation answer
  object that says which kind it is.
- **Exceptions are not converted into answers.** A handle that raises ends the
  fan-out the way it would end a direct call; the aggregate does not invent an
  answer shape for "this one handle failed", because that would be a resolver
  decision made outside a resolver.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Generic, Sequence, TypeVar

from .detect import detect
from .firmware import FirmwareAnswer, FirmwareIdentification
from .installations import (
    CatalogueAnswer,
    Health,
    Installation,
    LaunchabilityAnswer,
    PlatformSystemsAnswer,
    RomPlacement,
    SystemPlatformsAnswer,
    SystemsAnswer,
)
from .machine import Machine
from .placement import (
    ModPlacement,
    SavefilePlacement,
    SavestatePlacement,
    ScreenshotPlacement,
    SoftPatchAnswer,
    TexturePlacement,
    Unresolved,
)

AnswerT = TypeVar("AnswerT")


@dataclass(frozen=True, slots=True)
class InstallationAnswer(Generic[AnswerT]):
    """One installation's answer, and the handle that gave it.

    The pair travels together because the answer alone is ambiguous the moment
    more than one arrangement is installed: two save locations for one ROM are
    both true, and which is which is a fact about their origin. ``answer`` is
    whatever the question returns — a :class:`~atlas.placement.SavefilePlacement`,
    a :class:`~atlas.firmware.FirmwareAnswer`, a
    :class:`~atlas.installations.Health` — unchanged and unwrapped.
    """

    installation: Installation
    answer: AnswerT


class EveryInstallation:
    """Every detected installation, asked as one — fan-out, and nothing else.

    Mirrors the question set of the :class:`~atlas.installations.Installation`
    protocol: each method asks every handle its own version of the question and
    returns one :class:`InstallationAnswer` per handle, in detection order.
    Handles are live, so the aggregate is live too: each call re-asks, and each
    handle re-reads its own sources exactly as it does on the direct route.

    Built from what :func:`~atlas.detect.detect` found — see
    :func:`every_installation` for the one-call form. Taking the handles rather
    than a *home* is what keeps the aggregate thin: it never detects, so the
    answers are about the installations the caller already holds, and a caller
    who has chosen can keep using the handles directly.
    """

    def __init__(self, installations: Sequence[Installation]) -> None:
        self._installations = tuple(installations)

    @property
    def installations(self) -> tuple[Installation, ...]:
        """The handles being asked, in detection order — empty when none were found."""
        return self._installations

    def _ask(
        self, question: Callable[[Installation], AnswerT]
    ) -> tuple[InstallationAnswer[AnswerT], ...]:
        """Put one question to every handle and label each answer with its handle."""
        return tuple(
            InstallationAnswer(installation, question(installation))
            for installation in self._installations
        )

    def health(self) -> tuple[InstallationAnswer[Health], ...]:
        """Each installation's structured health — check before trusting its answers."""
        return self._ask(lambda installation: installation.health())

    def savefile_location(
        self,
        *,
        content_path: str | None = None,
        core_so: str | None = None,
        system: str | None = None,
    ) -> tuple[InstallationAnswer[SavefilePlacement | Unresolved], ...]:
        """Where each installation keeps this save — one placement per arrangement.

        An installation that does not have the named core refuses rather than
        answering, so an answer here is a placement *or* that refusal: the same
        question can be answerable on one arrangement and not on its neighbour,
        which is exactly what this route exists to show.
        """
        return self._ask(
            lambda installation: installation.savefile_location(
                content_path=content_path, core_so=core_so, system=system
            )
        )

    def savestate_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> tuple[InstallationAnswer[SavestatePlacement | Unresolved], ...]:
        """Where each installation keeps this content's savestates, or refuses to.

        Refuses on the same condition the savefile route does: a core this
        installation does not have.
        """
        return self._ask(
            lambda installation: installation.savestate_location(
                content_path=content_path, core_so=core_so
            )
        )

    def screenshot_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> tuple[InstallationAnswer[ScreenshotPlacement | Unresolved], ...]:
        """Where each installation keeps this content's screenshots, or refuses to.

        Refuses on the same condition the savefile route does: a core this
        installation does not have.
        """
        return self._ask(
            lambda installation: installation.screenshot_location(
                content_path=content_path, core_so=core_so
            )
        )

    def texture_pack_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> tuple[InstallationAnswer[TexturePlacement | Unresolved], ...]:
        """Where each installation's copy of this core reads texture packs from.

        The question this fan-out shows off best: two arrangements running the
        same core point it at two different roots, and both are true. An
        installation that does not have the core, or whose wiring atlas has not
        established, refuses instead of answering — so an answer here is a
        placement *or* one of those refusals.
        """
        return self._ask(
            lambda installation: installation.texture_pack_location(
                content_path=content_path, core_so=core_so
            )
        )

    def mod_location(
        self, *, content_path: str | None = None, core_so: str | None = None
    ) -> tuple[InstallationAnswer[ModPlacement | Unresolved], ...]:
        """Where each installation's copy of this core reads mods from.

        Two arrangements running one core point it at two roots, and both are
        true. An installation that does not have the core, or whose mod wiring
        atlas has not established, refuses instead of answering.
        """
        return self._ask(
            lambda installation: installation.mod_location(
                content_path=content_path, core_so=core_so
            )
        )

    def soft_patch_candidates(
        self, content_path: str, *, core_so: str | None = None
    ) -> tuple[InstallationAnswer[SoftPatchAnswer | Unresolved], ...]:
        """Which patch files each installation's RetroArch would apply to this content.

        The candidate *names* are the content's own and come back identical from
        every handle — what differs per arrangement is what each build was
        established to attempt, and whether the named core is installed there at
        all, which is the one condition this question refuses on.
        """
        return self._ask(
            lambda installation: installation.soft_patch_candidates(
                content_path, core_so=core_so
            )
        )

    def systems(self) -> tuple[InstallationAnswer[SystemsAnswer], ...]:
        """What each installation's frontend catalogue declares, or why it states nothing."""
        return self._ask(lambda installation: installation.systems())

    def systems_for_platform(
        self, vocabulary: str, value: str
    ) -> tuple[InstallationAnswer[PlatformSystemsAnswer], ...]:
        """Which of each installation's systems answer to a public platform id.

        The fan-out this question was made for: the same IGDB id lands on a
        declared system here, a disabled one there and nothing at all on the
        third arrangement — and all three are true, each with its status.
        """
        return self._ask(
            lambda installation: installation.systems_for_platform(vocabulary, value)
        )

    def platform_ids(self, system: str) -> tuple[InstallationAnswer[SystemPlatformsAnswer], ...]:
        """Each installation's platform tags and public identities for *system*."""
        return self._ask(lambda installation: installation.platform_ids(system))

    def emulators_for(
        self, system: str, *, content_path: str | None = None
    ) -> tuple[InstallationAnswer[CatalogueAnswer], ...]:
        """Which emulators each installation would launch *system* with."""
        return self._ask(
            lambda installation: installation.emulators_for(system, content_path=content_path)
        )

    def rom_location(self, system: str) -> tuple[InstallationAnswer[RomPlacement], ...]:
        """Where each installation keeps *system*'s ROMs, or why it states nowhere."""
        return self._ask(lambda installation: installation.rom_location(system))

    def launchable(
        self, system: str, content_path: str
    ) -> tuple[InstallationAnswer[LaunchabilityAnswer], ...]:
        """Whether each installation launches this file as *system* content, and why not."""
        return self._ask(lambda installation: installation.launchable(system, content_path))

    def firmware_for_core(
        self, core_so: str, *, verify: bool = False
    ) -> tuple[InstallationAnswer[FirmwareAnswer], ...]:
        """What this core wants, and where, under each installation's firmware root."""
        return self._ask(
            lambda installation: installation.firmware_for_core(core_so, verify=verify)
        )

    def firmware_for_system(
        self, system: str, *, verify: bool = False
    ) -> tuple[InstallationAnswer[FirmwareAnswer], ...]:
        """Which cores run this system under each installation, and what each wants."""
        return self._ask(
            lambda installation: installation.firmware_for_system(system, verify=verify)
        )

    def firmware_inventory(
        self, *, verify: bool = False
    ) -> tuple[InstallationAnswer[FirmwareAnswer], ...]:
        """Each installation's whole firmware tree — declared, present, and unclaimed."""
        return self._ask(lambda installation: installation.firmware_inventory(verify=verify))

    def identify_firmware(
        self, *, md5: str | None = None, sha1: str | None = None, size: int | None = None
    ) -> tuple[InstallationAnswer[FirmwareIdentification], ...]:
        """What this content is, and where each installation would want it placed."""
        return self._ask(
            lambda installation: installation.identify_firmware(md5=md5, sha1=sha1, size=size)
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"EveryInstallation({[i.kind for i in self._installations]})"


def every_installation(home: str, machine: Machine | None = None) -> EveryInstallation:
    """Detect the installations under *home* and return them ready to answer as one.

    The mirror of :func:`~atlas.detect.detect` for a caller who does not choose:
    same arguments, same probe order, same fixture-machine seam — the result
    just answers questions instead of being iterated. A caller who already holds
    detected handles wraps them with :class:`EveryInstallation` directly rather
    than detecting twice.
    """
    return EveryInstallation(detect(home, machine))
