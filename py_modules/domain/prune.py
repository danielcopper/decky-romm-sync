"""Pure naming, grouping, and option decisions for vanished-ROM cleanup."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, NotRequired, TypedDict

from domain.sibling_resolution import fs_name_stem, resolve_group_representative


class BundleGameRow(TypedDict):
    """One ROM the bundle covers, named for a human reading the folder."""

    rom_id: int
    name: str
    fs_name: str
    platform_slug: str
    role: str


class BundleReadmeContext(TypedDict):
    """Everything the generated README needs that the file records don't carry.

    Handed to the store instead of finished text: the ``files/NNNNNN`` mapping
    the index is built from only exists once the artifacts have been copied, so
    the README is rendered inside the seal rather than before it. It is the
    renderer's own input contract, which is why it lives beside the renderer.
    """

    bundle_id: str
    created_at: str
    games: list[BundleGameRow]
    playtime_lines: list[str]
    steam_app_id: NotRequired[int]


if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

    from domain.rom import Rom

_UNSAFE_PACKAGE_RUN = re.compile(r"[^A-Za-z0-9._-]+", re.ASCII)
_SAFE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$", re.ASCII)
_SAFE_SHORT_ID = re.compile(r"^[A-Za-z0-9]{4,32}$", re.ASCII)
_MAX_BUNDLE_NAME_CHARS = 64

# Every artifact kind the recovery producers emit, in the words a person reading
# the folder would use. An unmapped kind degrades to its raw slug in the README,
# so a new producer kind must land here in the same change.
_READABLE_KINDS = {
    "current_save": "current save file",
    "save_backup": "previous save (.romm-backup history)",
    "installed_rom": "downloaded ROM content",
    "steam_grid": "Steam grid artwork",
    "steam_input": "Steam Input configuration",
    "cover_cache": "cached cover image from RomM",
    "cover_validator": "cover freshness sidecar (ETag / Last-Modified)",
    "sgdb_cache": "cached SteamGridDB artwork",
}


def sanitize_package_name(name: object) -> str:
    """Return an ASCII path component suitable for the recovery-root name."""
    raw = name if isinstance(name, str) else ""
    cleaned = _UNSAFE_PACKAGE_RUN.sub("-", raw).strip("-._")
    return cleaned or "decky-plugin"


def sanitize_bundle_name(name: object) -> str:
    """Return a bounded ASCII path component for a game's name."""
    raw = name if isinstance(name, str) else ""
    cleaned = _UNSAFE_PACKAGE_RUN.sub("-", raw).strip("-._")[:_MAX_BUNDLE_NAME_CHARS].strip("-._")
    return cleaned or "game"


def recovery_bundle_id(game_name: object, date: str, short_id: str) -> str:
    """Build a bundle directory name a human can recognise months later.

    The recovery root is the manual-restore surface, so the folder leads with
    the game it holds. Uniqueness rides on ``short_id``, not on the name: two
    bundles for the same game on the same day differ only there, and the seal
    refuses to overwrite an existing directory either way.
    """
    if not _SAFE_DATE.fullmatch(date):
        raise ValueError("date must be an ISO YYYY-MM-DD day")
    if not _SAFE_SHORT_ID.fullmatch(short_id):
        raise ValueError("short_id must be 4-32 ASCII letters or digits")
    return f"{sanitize_bundle_name(game_name)}_{date}_{short_id}"


def render_bundle_readme(context: BundleReadmeContext, records: Sequence[Mapping[str, object]]) -> str:
    """Render the bundle's human index: what it holds and how to put it back.

    ``manifest.json`` stays the lossless machine authority; this is the same
    facts arranged for someone restoring by hand, which is the only kind of
    restore that exists.
    """
    games = context["games"]
    lines = [
        "decky-romm-sync recovery bundle",
        "===============================",
        "",
        f"Bundle:  {context['bundle_id']}",
        f"Created: {context['created_at']}",
        "",
        "A verified snapshot taken immediately BEFORE this game's local data was",
        "deleted. There is no automatic restore — everything here is put back by",
        "hand. manifest.json is the lossless machine-readable authority, and",
        "checksums.sha256 verifies every copied file.",
        "",
        "Games in this bundle",
        "--------------------",
    ]
    for game in games:
        lines.append(f"ROM {game['rom_id']} — {game['name']} [{game['platform_slug']}] — {game['role']}")
        if game["fs_name"]:
            lines.append(f"    file name on the server: {game['fs_name']}")
    app_id = context.get("steam_app_id")
    if app_id is not None:
        lines += [
            "",
            f"Steam shortcut appId {app_id} is recorded in manifest.json. Steam assigns",
            "appIds, so a rebuilt shortcut gets a new one and cannot inherit the old",
            "playtime — recreate it manually and re-point it at the game.",
        ]
    lines += ["", "Playtime", "--------", *context["playtime_lines"], "", "Files", "-----"]
    if not records:
        lines.append("This bundle copied no files; it records database state only.")
    for record in records:
        destination = record.get("destination")
        source = record.get("source_path")
        size = record.get("size")
        kind = _READABLE_KINDS.get(str(record.get("kind")), str(record.get("kind")))
        rom_id = record.get("rom_id")
        suffix = f" (ROM {rom_id})" if isinstance(rom_id, int) else ""
        lines.append(f"{destination}  {_human_size(size)}  {kind}{suffix}")
        lines.append(f"    restore to: {source}")
    lines += [
        "",
        "Restoring by hand",
        "-----------------",
        "1. Check the copies are intact — in this folder, run:",
        "       sha256sum -c checksums.sha256",
        "2. Put each file back at the path its entry names above, for example:",
        "       cp -a 'files/000001' '/path/from/the/entry'",
        "   Create any missing parent directories first, and do not overwrite a",
        "   newer file you want to keep.",
        "3. ROM files and saves are usable immediately once copied back.",
        "4. Database state (playtime, save-sync baselines, install records) lives in",
        "   manifest.json and is not restored by copying files. Re-syncing the game",
        "   in the plugin rebuilds its row; the recorded playtime is reference only.",
        "",
    ]
    return "\n".join(lines)


def _human_size(size: object) -> str:
    """Format a byte count for a person skimming the file table."""
    if not isinstance(size, int) or size < 0:
        return "unknown size"
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        value /= 1024
        if value < 1024:
            return f"{value:.1f} {unit}"
    return f"{value:.1f} PiB"


def group_rows(rows: Iterable[Rom]) -> list[list[Rom]]:
    """Group sibling rows; a NULL group key is always a singleton."""
    grouped: dict[str, list[Rom]] = {}
    singletons: list[list[Rom]] = []
    for row in rows:
        if row.sibling_group_key is None:
            singletons.append([row])
        else:
            grouped.setdefault(row.sibling_group_key, []).append(row)
    groups = [sorted(group, key=lambda row: row.rom_id) for group in grouped.values()]
    groups.extend(singletons)
    return sorted(groups, key=lambda group: group[0].rom_id)


def selected_prune_ids(
    *,
    group_ids: Sequence[int],
    candidate_ids: set[int],
    vanished_ids: set[int],
    live_ids: set[int],
    remove_rows: bool,
    remove_fully_vanished: bool,
) -> set[int]:
    """Select rows an option set permits after liveness has been established."""
    all_ids = set(group_ids)
    if all_ids and all_ids <= vanished_ids and not live_ids:
        return all_ids if remove_fully_vanished else set()
    if not live_ids or not remove_rows:
        return set()
    return candidate_ids & vanished_ids


def liveness_guard(
    verdicts: Mapping[int, Mapping[str, str]],
    delete_ids: set[int],
    target_id: int | None,
    vanished_source_id: int | None,
) -> tuple[str, str] | None:
    """Refuse the group unless every fresh verdict still supports what it plans.

    Reads a set of exact-ID verdicts and returns the ``(reason, message)`` the
    group is skipped with, or ``None`` when all of them hold. Each row the run
    would delete must still be ``vanished``, a repoint target must still be
    ``live``, and the vanished source a repoint moves the shortcut off must
    still be ``vanished`` — anything else (a resurrection, a disappearance, or
    an unproven answer) retains local data.
    """
    for rom_id in sorted(delete_ids):
        verdict = verdicts[rom_id]
        if verdict["status"] != "vanished":
            return verdict["reason"], f"ROM {rom_id}: {verdict['message']} Nothing else in this group was removed."
    if target_id is not None and verdicts[target_id]["status"] != "live":
        verdict = verdicts[target_id]
        return verdict["reason"], f"Default target {target_id}: {verdict['message']}"
    if vanished_source_id is not None and verdicts[vanished_source_id]["status"] != "vanished":
        verdict = verdicts[vanished_source_id]
        return verdict["reason"], f"Vanished source {vanished_source_id}: {verdict['message']}"
    return None


def natural_default(rows: Iterable[Rom], live_ids: set[int], preferred_region: str) -> int | None:
    """Pick the live row a repointed shortcut should bind to, or ``None``.

    Runs the same representative resolution the library sync uses, restricted to
    the group's live rows and with no installed/bound bias — the shortcut is
    about to move, so what it points at today may not influence where it lands.
    ``None`` when the group's live rows cannot yield one.
    """
    candidates = [
        {
            "rom_id": row.rom_id,
            "is_main_sibling": row.is_main_sibling,
            "regions": list(row.regions),
            "revision": row.revision,
            "tags": list(row.tags),
            "fs_name_no_ext": fs_name_stem(row.fs_name),
        }
        for row in rows
        if row.rom_id in live_ids
    ]
    try:
        return resolve_group_representative(
            candidates,
            installed_rom_ids=set(),
            bound_rom_ids=set(),
            preferred_region=preferred_region,
        )
    except (KeyError, ValueError):
        return None
