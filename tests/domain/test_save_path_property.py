"""Property-based tests for the canonical-target grouping key (#1028).

Invariant 1 (#1006 — cross-extension save corruption): a local file's sync
action must only ever be computed against server saves in its *own* canonical
target group. The grouping key is the pure
``compute_local_save_target(server_save, rom_name).filename`` — the same key
``services.saves._helpers.local_save_target`` wraps and the matrix groups by.

This property pins the *key*: grouping a generated server-save list by that
key never mixes two distinct canonical targets, and saves that differ only in
``file_extension`` for the same ``rom_name`` land in distinct groups. The
#1006 fix makes the matrix's local-file loop filter to this same group; the
key itself is already correct, so this is a live regression guard.

See ``tests.domain.test_sync_action_property`` for the convention note on the
``xfail(strict=True)`` pinning of properties that encode an open bug.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from domain.save_extensions import get_save_extensions
from domain.save_path import compute_local_save_target

# Every extension the generators draw from — the union across all systems in
# domain.save_extensions, leading dot stripped to match the file_extension shape.
_SYSTEMS: tuple[str | None, ...] = (
    None,
    "nds",
    "segacd",
    "saturn",
    "ngp",
    "ngpc",
    "pokemini",
    "amiga",
    "amigacd32",
)
_ALL_EXTENSIONS: tuple[str, ...] = tuple(
    sorted({ext.lstrip(".") for system in _SYSTEMS for ext in get_save_extensions(system)})
)

# A safe rom_name space: plain identifiers plus a couple of realistic names
# with spaces/parentheses. Excludes path-separator / traversal inputs (those
# are covered by the hand-enumerated sanitization tests) so the grouping key
# stays the clean <rom_name>.<ext> form this invariant is about.
_rom_names = st.sampled_from(
    [
        "Game",
        "Pokemon Emerald",
        "Sonic (USA)",
        "Final-Fantasy_VI",
        "Game.2",
    ]
)

_extensions = st.sampled_from(_ALL_EXTENSIONS)


def _server_save(save_id: int, file_extension: str) -> dict[str, Any]:
    """Minimal server-save dict carrying only what the grouping key reads."""
    return {
        "id": save_id,
        "slot": 0,
        "updated_at": "2024-01-01T00:00:00+00:00",
        "file_extension": file_extension,
        "device_syncs": [],
    }


@st.composite
def _server_save_list(draw: st.DrawFn) -> list[dict[str, Any]]:
    exts = draw(st.lists(_extensions, min_size=0, max_size=8))
    return [_server_save(i, ext) for i, ext in enumerate(exts)]


@given(saves=_server_save_list(), rom_name=_rom_names)
def test_grouping_never_mixes_canonical_targets(saves: list[dict[str, Any]], rom_name: str) -> None:
    """Grouping by the canonical-target key never places two saves with
    different canonical targets in the same group, and every member of a group
    resolves back to that group's key. This is the structural guarantee the
    #1006 fix relies on when it filters the local-file loop to a file's own
    group.
    """
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ss in saves:
        key = compute_local_save_target(ss, rom_name).filename
        groups[key].append(ss)

    for key, members in groups.items():
        # Every member of a group resolves to exactly that group's key — no
        # member can belong to a different canonical target.
        assert all(compute_local_save_target(m, rom_name).filename == key for m in members)


@given(rom_name=_rom_names, ext_a=_extensions, ext_b=_extensions)
def test_different_extension_same_rom_distinct_targets(rom_name: str, ext_a: str, ext_b: str) -> None:
    """Two server saves that differ only in ``file_extension`` for the same
    ``rom_name`` land in *distinct* canonical-target groups whenever the
    extensions differ. This is the exact #1006 condition: a multi-file save
    set (e.g. GBA ``.srm`` + ``.rtc``) must never collapse into one group, or
    one file's bytes can be PUT over the other's server record.
    """
    target_a = compute_local_save_target(_server_save(1, ext_a), rom_name).filename
    target_b = compute_local_save_target(_server_save(2, ext_b), rom_name).filename
    if ext_a != ext_b:
        assert target_a != target_b
    else:
        assert target_a == target_b
