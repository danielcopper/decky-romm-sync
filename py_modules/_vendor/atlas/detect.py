"""Detection — what emulator installations are present on a machine.

``detect(home, machine)`` probes the config markers of each known arrangement
and returns a handle for every one it finds. Detection labels markers, it does
not partition: EmuDeck *is* a configured ``org.libretro.RetroArch``, so the
EmuDeck marker is checked before concluding "a bare RetroArch", and the EmuDeck
handle claims that Flatpak (its ``kinds`` carries both descriptions —
no second handle for the same RetroArch). Multiple arrangements coexist
ordinarily, so the result is a list, highest-priority first: RetroDECK, EmuDeck,
the bare RetroArch Flatpak, a bare native RetroArch. Ambiguity is a truthful
result — the caller chooses, atlas never silently picks a winner.

Every probe goes through the machine seam, so detection is fully provable from a
fixture machine.
"""

from __future__ import annotations

import os

from .installations import (
    EMUDECK_SETTINGS_SUFFIX,
    NATIVE_CFG_SUFFIX,
    RETRODECK_JSON_SUFFIX,
    STANDALONE_FLATPAK_CFG_SUFFIX,
    EmuDeck,
    Installation,
    BareRetroArchNative,
    RetroDeck,
    BareRetroArchFlatpak,
)
from .machine import KIND_MISSING, Machine, RealMachine


def detect(home: str, machine: Machine | None = None) -> list[Installation]:
    """Detect the emulator installations under *home*.

    *machine* defaults to the real machine; tests and conformance vectors pass a
    fixture. Returns the detected installations in probe order (an empty list
    when nothing is installed).
    """
    if machine is None:
        machine = RealMachine()

    found: list[Installation] = []

    # A marker that exists but cannot be read or parsed is a PRESENT, broken
    # installation — detection triggers on existence; health states the rest
    # (REVIEW H10). The handles are live: they re-read their sources per query.
    if machine.path_kind(os.path.join(home, RETRODECK_JSON_SUFFIX)) != KIND_MISSING:
        found.append(RetroDeck(home, machine))

    emudeck_present = machine.path_kind(os.path.join(home, EMUDECK_SETTINGS_SUFFIX)) != KIND_MISSING
    if emudeck_present:
        found.append(EmuDeck(home, machine))

    # EmuDeck claims the bare Flatpak — same RetroArch, two descriptions,
    # one handle. Only an unclaimed Flatpak appears as its own installation.
    if not emudeck_present and machine.path_kind(os.path.join(home, STANDALONE_FLATPAK_CFG_SUFFIX)) != KIND_MISSING:
        found.append(BareRetroArchFlatpak(home, machine))

    if machine.path_kind(os.path.join(home, NATIVE_CFG_SUFFIX)) != KIND_MISSING:
        found.append(BareRetroArchNative(home, machine))

    return found
