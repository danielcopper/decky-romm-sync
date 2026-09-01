"""The command line — one question per invocation, its contract JSON on stdout.

This is the process boundary that makes atlas consumable from any language: a
client runs a subprocess and parses JSON. The output is exactly the contract
serialization the vector corpus already pins (:mod:`atlas.contract`) — the CLI
adds no dialect of its own, and ``tests/test_cli.py`` drives every vector
through this module's dispatch to hold it to that.

Two answer forms, both vector-pinned. Without ``--installation`` the question
is put to every detected installation and the answer is the labelled list
(:func:`~atlas.contract.installation_answers_contract`); with
``--installation <kind>`` the first handle of that kind answers alone, in the
question's bare contract shape.

The exit code separates answering from asking: 0 is an answer — an
``unresolved`` payload and an empty list are answers — and 2 is a question
that could not be put (usage errors, an ``--installation`` kind this machine
does not have). Nothing else rides the exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any, Callable, Sequence

from . import __version__
from .contract import (
    catalogue_contract,
    firmware_contract,
    health_contract,
    identification_contract,
    installation_answers_contract,
    installation_contract,
    launchable_contract,
    mod_answer_contract,
    platform_systems_contract,
    rom_placement_contract,
    savefile_answer_contract,
    savestate_answer_contract,
    screenshot_answer_contract,
    soft_patch_answer_contract,
    system_platforms_contract,
    systems_contract,
    texture_answer_contract,
)
from .platforms import KNOWN_PLATFORM_VOCABULARIES
from .detect import detect
from .every_installation import EveryInstallation
from .installations import Installation
from .machine import Machine

# A question is asked of one handle or of the aggregate, and the two surfaces
# are the same on purpose (EveryInstallation mirrors the Installation protocol
# method for method) — so one asker serves both targets, and the serializer is
# the contract function the question already has. The aggregate wrapper adds
# only the label, through installation_answers_contract.
_Target = Installation | EveryInstallation
_Ask = Callable[[_Target, argparse.Namespace], Any]
_Serialize = Callable[[Any], dict[str, Any]]

_QUESTIONS: dict[str, tuple[_Ask, _Serialize]] = {
    "health": (lambda target, _args: target.health(), health_contract),
    "savefile-location": (
        lambda target, args: target.savefile_location(
            content_path=args.content, core_so=args.core, system=args.system
        ),
        savefile_answer_contract,
    ),
    "savestate-location": (
        lambda target, args: target.savestate_location(
            content_path=args.content, core_so=args.core
        ),
        savestate_answer_contract,
    ),
    "screenshot-location": (
        lambda target, args: target.screenshot_location(
            content_path=args.content, core_so=args.core
        ),
        screenshot_answer_contract,
    ),
    "texture-pack-location": (
        lambda target, args: target.texture_pack_location(
            content_path=args.content, core_so=args.core
        ),
        texture_answer_contract,
    ),
    "mod-location": (
        lambda target, args: target.mod_location(content_path=args.content, core_so=args.core),
        mod_answer_contract,
    ),
    "soft-patch-candidates": (
        lambda target, args: target.soft_patch_candidates(args.content, core_so=args.core),
        soft_patch_answer_contract,
    ),
    "systems": (lambda target, _args: target.systems(), systems_contract),
    "systems-for-platform": (
        lambda target, args: target.systems_for_platform(args.vocabulary, args.value),
        platform_systems_contract,
    ),
    "platform-ids": (
        lambda target, args: target.platform_ids(args.system),
        system_platforms_contract,
    ),
    "emulators-for": (
        lambda target, args: target.emulators_for(args.system, content_path=args.content),
        catalogue_contract,
    ),
    "rom-location": (lambda target, args: target.rom_location(args.system), rom_placement_contract),
    "launchable": (
        lambda target, args: target.launchable(args.system, args.content),
        launchable_contract,
    ),
    "firmware-for-core": (
        lambda target, args: target.firmware_for_core(args.core, verify=args.verify),
        firmware_contract,
    ),
    "firmware-for-system": (
        lambda target, args: target.firmware_for_system(args.system, verify=args.verify),
        firmware_contract,
    ),
    "firmware-inventory": (
        lambda target, args: target.firmware_inventory(verify=args.verify),
        firmware_contract,
    ),
    "identify-firmware": (
        lambda target, args: target.identify_firmware(md5=args.md5, sha1=args.sha1, size=args.size),
        identification_contract,
    ),
}


# Help texts shared by every subcommand that takes the same input.
_SYSTEM_ID_HELP = "system id, e.g. gba"
_VERIFY_HELP = "hash present files against known dumps"


def build_parser() -> argparse.ArgumentParser:
    """One subcommand per existing question, the question's own inputs as flags.

    The holes a question leaves open in the library (``content_path=None``,
    ``core_so=None``) stay open here — the CLI validates nothing the library
    does not, so an underspecified question gets the library's own answer to
    it, not a usage error.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--home", help="answer for this home directory instead of the current user's"
    )
    selecting = argparse.ArgumentParser(add_help=False)
    selecting.add_argument(
        "--installation",
        help="ask only the first detected installation of this kind (e.g. retrodeck); "
        "default: every detected installation answers, labelled",
    )

    parser = argparse.ArgumentParser(
        prog="emu-atlas",
        description="Ask atlas one question; the answer is its contract JSON on stdout.",
    )
    # `--version` answers with the bare version string — an answer, exit 0 —
    # so a script captures it without stripping a prog prefix.
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser(
        "detect", parents=[common], help="the installations present on this machine"
    )
    commands.add_parser(
        "health", parents=[common, selecting], help="each installation's structured health"
    )

    def content_core(name: str, help_text: str, *, system: bool = False) -> None:
        question = commands.add_parser(name, parents=[common, selecting], help=help_text)
        question.add_argument("--content", help="path of the content file being asked about")
        question.add_argument("--core", help="libretro core .so name, e.g. mgba_libretro.so")
        if system:
            question.add_argument("--system", help="system the content is filed under")

    content_core("savefile-location", "where this content's save file lands", system=True)
    content_core("savestate-location", "where this content's savestates land")
    content_core("screenshot-location", "where this content's screenshots land")
    content_core("texture-pack-location", "where this core reads texture packs from")
    content_core("mod-location", "where this core reads mods from")

    soft_patch = commands.add_parser(
        "soft-patch-candidates",
        parents=[common, selecting],
        help="which patch files RetroArch would apply to this content",
    )
    soft_patch.add_argument("content", help="path of the content file")
    soft_patch.add_argument("--core", help="libretro core .so name, e.g. mgba_libretro.so")

    commands.add_parser(
        "systems", parents=[common, selecting], help="what the frontend catalogue declares"
    )

    for_platform = commands.add_parser(
        "systems-for-platform",
        parents=[common, selecting],
        help="which systems here answer to a public platform id",
    )
    for_platform.add_argument(
        "vocabulary",
        choices=KNOWN_PLATFORM_VOCABULARIES,
        help="the public vocabulary the id comes from",
    )
    for_platform.add_argument(
        "value", help="the id in that vocabulary, e.g. an IGDB slug or numeric id"
    )

    platform_ids = commands.add_parser(
        "platform-ids",
        parents=[common, selecting],
        help="a system's platform tags and their public identities",
    )
    platform_ids.add_argument("system", help=_SYSTEM_ID_HELP)

    emulators = commands.add_parser(
        "emulators-for",
        parents=[common, selecting],
        help="which emulators would launch this system",
    )
    emulators.add_argument("system", help=_SYSTEM_ID_HELP)
    emulators.add_argument("--content", help="path of the content file being asked about")

    rom = commands.add_parser(
        "rom-location", parents=[common, selecting], help="where this system's ROMs are kept"
    )
    rom.add_argument("system", help=_SYSTEM_ID_HELP)

    launch = commands.add_parser(
        "launchable",
        parents=[common, selecting],
        help="whether this file launches as this system's content, and why not",
    )
    launch.add_argument("system", help="system id, e.g. dreamcast")
    launch.add_argument("content", help="path of the content file")

    for_core = commands.add_parser(
        "firmware-for-core",
        parents=[common, selecting],
        help="what this core wants under the firmware root",
    )
    for_core.add_argument("--core", required=True, help="libretro core .so name")
    for_core.add_argument("--verify", action="store_true", help=_VERIFY_HELP)

    for_system = commands.add_parser(
        "firmware-for-system",
        parents=[common, selecting],
        help="which cores run this system, and what each wants",
    )
    for_system.add_argument("--system", required=True, help=_SYSTEM_ID_HELP)
    for_system.add_argument("--verify", action="store_true", help=_VERIFY_HELP)

    inventory = commands.add_parser(
        "firmware-inventory",
        parents=[common, selecting],
        help="the whole firmware tree — declared, present, unclaimed",
    )
    inventory.add_argument("--verify", action="store_true", help=_VERIFY_HELP)

    identify = commands.add_parser(
        "identify-firmware",
        parents=[common, selecting],
        help="what this file is, and where it would be wanted",
    )
    identify.add_argument("--md5", help="md5 of the file being identified")
    identify.add_argument("--sha1", help="sha1 of the file being identified")
    identify.add_argument("--size", type=int, help="size in bytes of the file being identified")

    return parser


def run(argv: Sequence[str], *, home: str | None = None, machine: Machine | None = None) -> int:
    """Ask the one question *argv* names and write its contract JSON to stdout.

    *home* and *machine* are the same seams :func:`atlas.detect.detect` has —
    the conformance tests bind a fixture machine here, :func:`main` binds
    nothing and lets the real machine answer. ``--home`` on the command line
    wins over the *home* argument.
    """
    args = build_parser().parse_args(argv)
    where = args.home if args.home is not None else home
    installations = detect(where if where is not None else os.path.expanduser("~"), machine)

    if args.command == "detect":
        payload: object = [installation_contract(i) for i in installations]
    else:
        ask, serialize = _QUESTIONS[args.command]
        if args.installation is None:
            payload = installation_answers_contract(
                ask(EveryInstallation(installations), args), serialize
            )
        else:
            chosen = next((i for i in installations if i.kind == args.installation), None)
            if chosen is None:
                print(
                    f"no detected installation of kind '{args.installation}'"
                    f" — `emu-atlas detect` lists what this machine has",
                    file=sys.stderr,
                )
                return 2
            payload = serialize(ask(chosen, args))

    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0


def main() -> int:
    """The installed entry point — the real machine, the real home."""
    return run(sys.argv[1:])
