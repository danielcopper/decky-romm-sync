"""BIOS readiness — the status shape every surface renders, and the one level over it.

Domain owns the unknown/ok/partial/missing LEVEL (``compute_bios_level``) and the
compact status token (``compute_bios_label``) — the single source of truth for
the readiness decision the game-detail panel, the play-section row and the System
page all read. Verbose per-surface phrasing and the status-dot colour are UI
concerns and deliberately do NOT live here.

Two axes run through this module and must not be folded into one. **What wants a
file** is :mod:`domain.firmware_wants`' four-valued answer, and it is a property
of the machine: the same file answers the same way on every surface. **Whether
the game in front of the user is ready to launch** is scoped to the core it will
launch with, which is why an entry carries ``required_by_active`` beside its
``wanted`` and why the counts key off the first. A file three other cores demand
is not a missing prerequisite for this launch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from domain.firmware_wants import WANTED_NEEDED, WANTED_OPTIONAL, WANTED_UNKNOWN, classify_wanted

if TYPE_CHECKING:
    from collections.abc import Mapping

    from domain.firmware_wants import FirmwarePlacement

BIOS_LEVEL_UNKNOWN = "unknown"
BIOS_LEVEL_OK = "ok"
BIOS_LEVEL_PARTIAL = "partial"
BIOS_LEVEL_MISSING = "missing"

# The compact token :func:`compute_bios_label` answers for the unknown level. A
# constant because a caller that ships the level without going through the
# function still has to name the label that goes with it.
BIOS_LABEL_UNKNOWN = "Unknown"


@dataclass(frozen=True)
class AvailableCore:
    """A RetroArch core available for a platform."""

    core_so: str
    label: str
    is_default: bool


@dataclass(frozen=True)
class BiosFileEntry:
    """Status of a single BIOS/firmware file on a platform's list.

    ``wanted`` is the machine's answer about the file (one of
    :data:`domain.firmware_wants.WANTED_VALUES`); ``required_by_active`` is the
    launching core's, and only that one decides whether the file is counted as a
    missing prerequisite.

    ``on_server`` is clear for a file an installed emulator asks for that the
    RomM library does not hold. Such a file is real, and missing, and nothing on
    this page can fetch it — so it is shown, and it is kept out of every count
    that offers the user an action.
    """

    file_name: str
    downloaded: bool
    local_path: str
    description: str
    wanted: str
    required_by_active: bool
    cores: dict[str, dict[str, Any]]  # {core_so: {"required": bool}}
    used_by_active: bool
    on_server: bool = True


@dataclass(frozen=True)
class BiosStatus:
    """Aggregated BIOS status for a platform, ready for frontend display."""

    platform_slug: str
    server_count: int
    local_count: int
    all_downloaded: bool
    required_count: int | None
    required_downloaded: int | None
    files: tuple[BiosFileEntry, ...]
    active_core: str | None
    active_core_label: str | None
    available_cores: tuple[AvailableCore, ...]
    # Server files the machine has an answer about (``needed`` or ``optional``).
    # ``None`` means the caller did not supply it, so the "unknown" decision is
    # not made; ``0`` alongside ``unknown_count`` means nothing about this
    # platform's firmware could be established at all.
    known_count: int | None = None
    unknown_count: int = 0
    # Whether an absence from this platform's file list may be read as "nothing
    # wants it" — ``FirmwareCatalogue.reading_complete_for`` scoped to the
    # emulators the platform offers. Defaults to ``True`` so a caller that does
    # not supply it keeps the level it always got; the one decision it moves is
    # a platform with no files at all.
    reading_complete: bool = True
    cached_at: float = 0.0


def format_bios_status(
    bios: dict[str, Any],
    platform_slug: str,
    *,
    reading_complete: bool = True,
    cached_at: float = 0.0,
) -> BiosStatus:
    """Build a frontend-ready BiosStatus dataclass from raw firmware check result."""
    raw_files = bios.get("files", [])
    if raw_files and isinstance(raw_files[0], dict):
        files: tuple[BiosFileEntry, ...] = tuple(
            BiosFileEntry(
                file_name=f.get("file_name", ""),
                downloaded=f.get("downloaded", False),
                local_path=f.get("local_path", ""),
                description=f.get("description", ""),
                wanted=f.get("wanted", WANTED_UNKNOWN),
                required_by_active=f.get("required_by_active", False),
                cores=f.get("cores", {}),
                used_by_active=f.get("used_by_active", True),
                on_server=f.get("on_server", True),
            )
            for f in raw_files
        )
    else:
        files = tuple(raw_files)

    raw_cores = bios.get("available_cores", [])
    available_cores: tuple[AvailableCore, ...] = tuple(
        AvailableCore(
            core_so=c.get("core_so", c.get("core", "")),
            label=c.get("label", ""),
            is_default=c.get("is_default", False),
        )
        for c in raw_cores
    )

    return BiosStatus(
        platform_slug=platform_slug,
        server_count=bios.get("server_count", 0),
        local_count=bios.get("local_count", 0),
        all_downloaded=bios.get("all_downloaded", False),
        required_count=bios.get("required_count"),
        required_downloaded=bios.get("required_downloaded"),
        files=files,
        active_core=bios.get("active_core"),
        active_core_label=bios.get("active_core_label"),
        available_cores=available_cores,
        known_count=bios.get("known_count"),
        unknown_count=bios.get("unknown_count", 0),
        reading_complete=reading_complete,
        cached_at=cached_at,
    )


def build_file_entry(
    file_name: str,
    downloaded: bool,
    dest: str,
    placement: FirmwarePlacement | None,
    complete: bool,
    active_core_so: str | None,
    *,
    on_server: bool = True,
) -> BiosFileEntry:
    """Build a single file status entry from the machine's answer about it.

    ``placement`` is the catalogue's entry for the file (``None`` when nothing
    declares it) and ``complete`` the reading state for the platform's own
    emulators — together they decide ``wanted``. ``active_core_so`` is the core
    the game will launch with, or ``None`` when it could not be resolved; then
    every declaring core stands in for it, which is the same permissive default
    the platform has always fallen back to.
    """
    wants = placement.wants if placement is not None else ()
    cores = {want.core_so: {"required": want.required} for want in wants if want.core_so is not None}
    if active_core_so is None:
        used_by_active = True
        required_by_active = placement.required_by_any if placement is not None else False
    else:
        used_by_active = active_core_so in cores if cores else True
        required_by_active = cores.get(active_core_so, {}).get("required", False)
    return BiosFileEntry(
        file_name=file_name,
        downloaded=downloaded,
        local_path=dest,
        description=placement.description if placement is not None else file_name,
        wanted=classify_wanted(placement, complete),
        required_by_active=required_by_active,
        cores=cores,
        used_by_active=used_by_active,
        on_server=on_server,
    )


def collect_firmware_status(
    items: list[dict[str, Any]],
    placements: Mapping[str, FirmwarePlacement],
    complete: bool,
    active_core_so: str | None,
) -> tuple[BiosFileEntry, ...]:
    """Build BiosFileEntry objects for a list of pre-resolved firmware items.

    Each item must have keys: file_name, downloaded, dest; ``on_server``
    defaults to ``True`` for the items that came off the RomM listing.
    """
    return tuple(
        build_file_entry(
            item["file_name"],
            item["downloaded"],
            item["dest"],
            placements.get(item["file_name"]),
            complete,
            active_core_so,
            on_server=item.get("on_server", True),
        )
        for item in items
    )


def count_required(files: tuple[BiosFileEntry, ...]) -> tuple[int, int]:
    """``(required, of those downloaded)`` for the core the game will launch with.

    The badge's two numbers, derived in one place so the System page and the
    game-detail page can never disagree about which files count. A file the
    library does not hold counts here — it is genuinely required and genuinely
    absent, and excluding it would make the badge read ready for a game that
    cannot launch. What it must never do is imply a download; whether anything
    is fetchable is a separate question, asked where the buttons are.
    """
    required = [f for f in files if f.required_by_active]
    return len(required), sum(1 for f in required if f.downloaded)


def _nothing_established(status: BiosStatus) -> bool:
    """Nothing about this platform's firmware could be established.

    Two shapes, and the second is why ``reading_complete`` exists. The server
    holds firmware and the machine answered for none of it — every row unknown,
    so there is nothing to base a claim on. Or the platform has no file at all
    AND its reading never happened: an empty list under a *complete* reading is
    the finished answer "no emulator here wants anything", while under an
    incomplete one it is silence. Silence read as an answer is the green "All
    ready" a platform whose only emulator is standalone used to show over
    firmware that emulator will not boot without.

    ``known_count is None`` is a caller that did not supply the counts, and the
    decision is then left to the required-count logic as it always was.
    """
    if status.known_count is None:
        return False
    if not status.reading_complete and not status.files:
        return True
    return status.server_count > 0 and status.known_count == 0 and status.unknown_count > 0


def compute_bios_level(status: BiosStatus) -> str:
    """Compute BIOS status level: 'unknown', 'ok', 'partial', or 'missing'.

    ``'unknown'`` means no readiness claim can be made — see
    :func:`_nothing_established` for the two shapes that reach it. It is checked
    first, before the required-count logic, and only fires when the caller
    supplied ``known_count`` (else the decision is deferred to the existing
    ok/partial/missing logic). Both shapes imply ``required_count == 0`` — one
    answers for no file, the other holds none — so it can only ever displace a
    false ``'ok'``, never mask a real ``'missing'``.

    A platform whose files are all *answered for* and wanted by nothing is a
    different case entirely and reaches ``'ok'``: "no emulator here needs these"
    is a finished answer, and the file rows say which files it covers.
    """
    if _nothing_established(status):
        return BIOS_LEVEL_UNKNOWN
    req_count = status.required_count
    req_done = status.required_downloaded
    if req_count is not None and req_done is not None:
        if req_done >= req_count:
            return BIOS_LEVEL_OK
        if req_done > 0:
            return BIOS_LEVEL_PARTIAL
        return BIOS_LEVEL_MISSING
    if status.all_downloaded:
        return BIOS_LEVEL_OK
    if (status.local_count or 0) > 0:
        return BIOS_LEVEL_PARTIAL
    return BIOS_LEVEL_MISSING


def compute_bios_label(status: BiosStatus) -> str:
    """Compute the compact BIOS status token (verbose phrasing stays per-surface)."""
    if _nothing_established(status):
        return BIOS_LABEL_UNKNOWN
    req_count = status.required_count
    req_done = status.required_downloaded
    if req_count is not None and req_done is not None:
        if req_done >= req_count:
            return "OK"
        if req_done > 0:
            return f"{req_done}/{req_count} required"
        return "Missing"
    if status.all_downloaded:
        return "OK"
    if (status.local_count or 0) > 0:
        return f"{status.local_count}/{status.server_count}"
    return "Missing"


def count_wanted(files: tuple[BiosFileEntry, ...]) -> tuple[int, int]:
    """``(known, unknown)`` over the server's files — asked for, and unanswerable.

    A ``not_needed`` file is in neither: the machine answered for it, and no
    emulator asks for it. Its absence from both counts is what keeps
    ``compute_bios_level`` from reading "nothing here is needed" as "nothing
    could be established".

    **Scoped to ``on_server`` rows**, because ``_nothing_established`` weighs
    ``known_count`` against ``server_count``, which is the server's rows alone.
    A row the library does not hold exists only because an emulator declared the
    file, so it always classifies ``needed``/``optional`` and would always be
    counted as known: one such row would cancel the ``unknown`` verdict for a
    platform whose every server file went unanswered, turning the headline green
    while each row still said nothing could answer for it.
    """
    on_server = [f for f in files if f.on_server]
    known = sum(1 for f in on_server if f.wanted in (WANTED_NEEDED, WANTED_OPTIONAL))
    unknown = sum(1 for f in on_server if f.wanted == WANTED_UNKNOWN)
    return known, unknown
