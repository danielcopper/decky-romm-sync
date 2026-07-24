"""Pure naming, grouping, and option decisions for vanished-ROM cleanup."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from domain.rom import Rom

_UNSAFE_PACKAGE_RUN = re.compile(r"[^A-Za-z0-9._-]+", re.ASCII)
_SAFE_UUID = re.compile(r"^[A-Za-z0-9-]+$", re.ASCII)


def sanitize_package_name(name: object) -> str:
    """Return an ASCII path component suitable for the recovery-root name."""
    raw = name if isinstance(name, str) else ""
    cleaned = _UNSAFE_PACKAGE_RUN.sub("-", raw.encode("ascii", "ignore").decode("ascii")).strip("-._")
    return cleaned or "decky-plugin"


def recovery_bundle_id(timestamp: str, lowest_rom_id: int, operation_id: str) -> str:
    """Build a collision-resistant bundle directory name from trusted values."""
    if lowest_rom_id <= 0:
        raise ValueError("lowest_rom_id must be positive")
    if not _SAFE_UUID.fullmatch(operation_id):
        raise ValueError("operation_id must contain only ASCII letters, digits, and hyphens")
    safe_timestamp = re.sub(r"[^0-9TZ]", "", timestamp, flags=re.ASCII)
    if not safe_timestamp:
        raise ValueError("timestamp must contain a UTC date/time")
    return f"{safe_timestamp}_{lowest_rom_id}_{operation_id}"


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
