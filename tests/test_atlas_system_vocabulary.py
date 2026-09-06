"""The plugin's platform map speaks the system names the vendored resolver knows.

``defaults/config.json``'s ``platform_map`` translates a RomM platform slug into a
system name, and every question this plugin puts to the resolver is keyed on that
name: the emulator catalogue, the accept-list, the ROM directory. A name outside
the resolver's vocabulary answers nothing everywhere at once, and it answers
quietly — an unknown system is a legitimate answer for a catalogue that was read.

So the vocabulary is checked here rather than discovered on a device. Two names
are known exceptions, listed rather than filtered so a third fails loudly.
"""

from __future__ import annotations

import json
import pathlib

from _vendor.atlas import known_systems

_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

# No ES-DE build declares a system for either machine, so the resolver's
# vocabulary — which is ES-DE's — carries no name for them. A ROM on one is
# placed under a directory ES-DE never scans; giving them a home is its own
# issue, not something this test may hide.
_NO_ES_DE_SYSTEM = frozenset({"atarijaguarcd", "xbox360"})


def _mapped_systems() -> set[str]:
    config = json.loads((_REPO_ROOT / "defaults" / "config.json").read_text(encoding="utf-8"))
    return set(config["platform_map"].values())


def test_every_mapped_system_is_one_the_resolver_knows() -> None:
    assert _mapped_systems() - set(known_systems()) == set(_NO_ES_DE_SYSTEM)


def test_the_exceptions_are_still_mapped() -> None:
    # A name dropped from the platform map must leave this list with it, or the
    # test above starts asserting an exception nothing claims any more.
    assert _mapped_systems() >= _NO_ES_DE_SYSTEM
