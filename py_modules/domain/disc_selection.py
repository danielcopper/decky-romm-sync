"""Multi-disc enumeration and launch-path resolution — pure decision kernel.

The disc-picker's compute layer: turn a flat list of files in a ROM's install
directory into an ordered, labelled list of launchable discs, and resolve which
one a launch should bake given the user's persisted selection. A "disc" is a
disc-image container (see :mod:`domain.disc_formats`) the emulator can launch
directly — never a ``.bin`` sidecar, never the ``.m3u`` playlist.

Resolution is a bake-time path-override layer only: it returns the path to bake
into the Steam shortcut's launch_options, it never rewrites the install's
``file_path`` (save-path / core / displayed-filename derivations stay stable),
mirroring how ``emulator_override`` overrides the invocation without touching
``file_path``. The NULL-selection default is the ``.m3u`` when the install's
``file_path`` is one (in-emulator disc swap), else the first enumerated disc.

No I/O, no service/adapter/lib imports. Pure functions only. See ADR-0014.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from domain.disc_formats import DISC_IMAGE_EXTENSIONS

# Disc-number tag inside a basename: "(Disc 1)", "[Disk 02]", "(Disc 1 of 2)".
# Case-insensitive; tolerates leading zeros so "Disc 01" and "Disc 1" agree.
_DISC_NUMBER_RE = re.compile(r"[\(\[]\s*dis[ck]\s*0*(\d+)", re.IGNORECASE)

# Sort key for discs whose number could not be parsed: they fall after every
# numbered disc, then order lexicographically among themselves.
_UNPARSED_ORDER = float("inf")


@dataclass(frozen=True, slots=True)
class Disc:
    """One launchable disc image within a multi-disc ROM's install directory.

    ``filename`` is the basename (the stable selection key persisted in
    ``roms.selected_disc``); ``path`` is the absolute path to bake; ``label``
    is the user-facing face ("Disc 1", or the basename stem when unparseable);
    ``index`` is the 1-based position in enumeration order.
    """

    filename: str
    path: str
    label: str
    index: int


def _parse_disc_number(basename: str) -> int | None:
    """Return the disc number tagged in *basename*, or ``None`` if untagged."""
    match = _DISC_NUMBER_RE.search(basename)
    return int(match.group(1)) if match is not None else None


def enumerate_discs(files: list[str], supported_extensions: frozenset[str] | None) -> list[Disc]:
    """Enumerate the launchable discs among *files*, in disc order.

    Parameters
    ----------
    files:
        Absolute paths to every file in the ROM's install directory (a flat,
        already-recursive listing). Non-disc files are ignored.
    supported_extensions:
        The live per-system extension accept-list from es_systems
        (lowercased), intersected with :data:`DISC_IMAGE_EXTENSIONS` so a disc
        the emulator cannot launch is never listed. ``None`` (es_systems
        unavailable) falls back to the full disc set.

    Returns
    -------
    list[Disc]
        Discs ordered by parsed disc number (numbered discs first, numerically
        — so ``Disc 2`` precedes ``Disc 10`` regardless of zero-padding), then
        any unparseable basenames lexicographically. ``index`` is 1-based in
        this order; ``label`` is ``"Disc N"`` when a number was parsed, else the
        basename without its extension.
    """
    effective = (DISC_IMAGE_EXTENSIONS & supported_extensions) if supported_extensions else DISC_IMAGE_EXTENSIONS

    candidates: list[tuple[int | float, str, str, int | None]] = []
    for path in files:
        basename = os.path.basename(path)
        if os.path.splitext(basename)[1].lower() not in effective:
            continue
        number = _parse_disc_number(basename)
        sort_number: int | float = number if number is not None else _UNPARSED_ORDER
        candidates.append((sort_number, basename.lower(), path, number))

    candidates.sort(key=lambda item: (item[0], item[1]))

    discs: list[Disc] = []
    for position, (_sort_number, _sort_name, path, number) in enumerate(candidates, start=1):
        basename = os.path.basename(path)
        label = f"Disc {number}" if number is not None else os.path.splitext(basename)[0]
        discs.append(Disc(filename=basename, path=path, label=label, index=position))
    return discs


def resolve_launch_path(file_path: str, discs: list[Disc], selected_disc: str | None) -> tuple[str, bool]:
    """Resolve the path to bake into launch_options for a (possibly pinned) ROM.

    Returns ``(path_to_bake, stale)``. ``stale`` is ``True`` when a non-NULL
    *selected_disc* no longer matches any enumerated disc (the pinned file is
    gone) — the caller degrades to the default and should log a WARNING, never
    fail. A non-multi-disc ROM (fewer than two discs) resolves to its own
    *file_path* unchanged.

    The default (no usable selection): the install's ``.m3u`` when *file_path*
    is one (in-emulator disc swap), else the first enumerated disc.
    """
    if len(discs) < 2:
        return (file_path, False)

    stale = False
    if selected_disc is not None:
        for disc in discs:
            if disc.filename == selected_disc:
                return (disc.path, False)
        stale = True

    if file_path.lower().endswith(".m3u"):
        return (file_path, stale)
    return (discs[0].path, stale)


def default_descriptor(file_path: str, discs: list[Disc]) -> dict[str, str]:
    """Describe the default (NULL-selection) target for the picker's UI.

    ``{"kind": "m3u"|"disc", "label": str, "filename": str}``. ``kind="m3u"``
    iff *file_path* is an ``.m3u`` (label ``"All discs (m3u)"``, filename the
    basename of *file_path*); otherwise ``kind="disc"`` and the label/filename
    of the first enumerated disc. Callers only reach here for multi-disc ROMs,
    so ``discs`` is non-empty in the ``"disc"`` branch.
    """
    if file_path.lower().endswith(".m3u"):
        return {"kind": "m3u", "label": "All discs (m3u)", "filename": os.path.basename(file_path)}
    first = discs[0]
    return {"kind": "disc", "label": first.label, "filename": first.filename}
