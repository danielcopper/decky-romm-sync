"""Property-based tests for ``domain.skip_prediction.collapsed_shortcut_count``.

The plan-time estimate kernel must mirror the LANE SELECTION of the real
collapse (``domain.sync_diff.collapse_sibling_groups``, ADR-0021) — a group
with bound siblings is grandfathered one-shortcut-per-bound-sibling (§5), an
unbound group mints one representative. The hand-enumerated cases
(``test_skip_prediction.py``) pin specific shapes; the cross-check property
here drives BOTH functions from the same sampled rows so the two can never
drift apart again (the #1382/#1402 undercount class).

Scope: the cross-check samples real (non-``None``) group keys only — a built
fetch entry always carries a real key, so ``collapse_sibling_groups`` never
sees a keyless member. The kernel's ``None`` branch models persisted
pre-backfill rows whose group is unknowable at plan time; its
one-singleton-per-row rule is pinned by a kernel-only additivity property.
"""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from domain.skip_prediction import collapsed_shortcut_count
from domain.sync_diff import collapse_sibling_groups

# One persisted row: (group key, bound?, installed?). A handful of key values
# is enough — the interesting structure is how rows cluster into groups and
# how many of each group's rows are bound.
_row = st.tuples(st.sampled_from(["g:a", "g:b", "g:c"]), st.booleans(), st.booleans())
_rows = st.lists(_row, max_size=12)


def _member(rom_id: int, key: str) -> dict[str, Any]:
    """A fetched shortcut entry, minimal but resolver-complete (rom_id, name,
    fs_name_no_ext; regions/revision/tags default empty via ``.get``)."""
    return {
        "rom_id": rom_id,
        "name": f"G{rom_id}",
        "fs_name": f"g{rom_id}.z64",
        "fs_name_no_ext": f"g{rom_id}",
        "platform_slug": "n64",
        "sibling_group_key": key,
        "launch_options": "",
    }


@given(rows=_rows)
def test_estimate_matches_real_collapse_emission_count(rows):
    """Same rows → the kernel's count equals ``len(collapse_sibling_groups(...))``.

    The scenario replayed is the plan-time premise: the next fetch returns
    exactly the persisted rows (every row fetched, complete platform view),
    bound rows carry their persisted binding, installed state arbitrary.
    """
    shortcuts_data = [_member(rom_id, key) for rom_id, (key, _bound, _installed) in enumerate(rows, start=1)]
    registry = {
        str(rom_id): {
            "app_id": 1000 + rom_id,
            "name": f"G{rom_id}",
            "fs_name": f"g{rom_id}.z64",
            "platform_slug": "n64",
            "sibling_group_key": key,
        }
        for rom_id, (key, bound, _installed) in enumerate(rows, start=1)
        if bound
    }
    installed_rom_ids = {rom_id for rom_id, (_key, _bound, installed) in enumerate(rows, start=1) if installed}

    emitted = collapse_sibling_groups(shortcuts_data, registry, installed_rom_ids, complete_group_view=True)
    estimated = collapsed_shortcut_count((key, bound) for key, bound, _installed in rows)

    assert len(emitted) == estimated


@given(rows=_rows, keyless=st.lists(st.booleans(), max_size=5))
def test_keyless_rows_add_one_singleton_each(rows, keyless):
    """Kernel-only ``None`` lane: k keyless rows (bound or not) add exactly k."""
    base = [(key, bound) for key, bound, _installed in rows]
    with_keyless = base + [(None, bound) for bound in keyless]
    assert collapsed_shortcut_count(with_keyless) == collapsed_shortcut_count(base) + len(keyless)
