"""Tests for ``domain.sync_chunking.build_unit_chunks`` — per-unit apply chunking.

The chunker slices a unit's emitted shortcuts into durable commit chunks. It is
pure and load-bearing for two safety properties: no sibling group is ever split
across a chunk boundary (so a group's rows always commit together), and every
fetched ROM lands in exactly one chunk's ``rom_ids`` (so each row commits once).
"""

from __future__ import annotations

from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from domain.sync_chunking import UnitChunk, build_unit_chunks


def _sd(rom_id: int, key: str | None) -> dict[str, Any]:
    return {"rom_id": rom_id, "sibling_group_key": key}


class TestBuildUnitChunks:
    def test_empty_emitted_yields_single_chunk_with_all_rom_ids(self):
        """No emitted shortcuts → one empty chunk carrying every ROM id.

        Preserves the empty-unit round-trip: the event still fires and the empty
        ack commits the unbound rows.
        """
        shortcuts_data = [_sd(1, "a"), _sd(2, "b")]
        chunks = build_unit_chunks([], shortcuts_data, 200)
        assert chunks == [UnitChunk(emitted=[], offset=0, rom_ids=[1, 2])]

    def test_small_unit_is_one_chunk(self):
        """A unit under the chunk size emits exactly one chunk at offset 0."""
        emitted = [_sd(1, "a"), _sd(2, "b")]
        chunks = build_unit_chunks(emitted, emitted, 200)
        assert len(chunks) == 1
        assert chunks[0].offset == 0
        assert chunks[0].emitted == emitted
        assert chunks[0].rom_ids == [1, 2]

    def test_singletons_split_freely_at_chunk_size(self):
        """``None``-key singletons never merge — they cut cleanly at the chunk size."""
        emitted = [_sd(i, None) for i in range(1, 5)]
        chunks = build_unit_chunks(emitted, list(emitted), 2)
        assert [len(c.emitted) for c in chunks] == [2, 2]
        assert [c.offset for c in chunks] == [0, 2]
        assert [c.rom_ids for c in chunks] == [[1, 2], [3, 4]]

    def test_group_run_never_split_across_chunks(self):
        """A sibling group's emitted entries stay whole even when they exceed the size."""
        emitted = [_sd(10, "g"), _sd(11, "g"), _sd(12, "g")]
        chunks = build_unit_chunks(emitted, list(emitted), 2)
        assert len(chunks) == 1
        assert [e["rom_id"] for e in chunks[0].emitted] == [10, 11, 12]

    def test_chunk_overflows_past_size_to_finish_a_group(self):
        """A group started under the limit finishes in the same chunk, overflowing it."""
        # chunk_size 3: two singletons then a 3-member group all land in chunk 0
        # (started at len 2 < 3, so the whole group is added → 5), then the next
        # singleton opens chunk 1.
        emitted = [_sd(1, "a"), _sd(2, "b"), _sd(10, "g"), _sd(11, "g"), _sd(12, "g"), _sd(3, "c")]
        chunks = build_unit_chunks(emitted, list(emitted), 3)
        assert [len(c.emitted) for c in chunks] == [5, 1]
        assert [e["rom_id"] for e in chunks[0].emitted] == [1, 2, 10, 11, 12]
        assert [e["rom_id"] for e in chunks[1].emitted] == [3]
        assert [c.offset for c in chunks] == [0, 5]

    def test_chunk_rom_ids_cover_every_group_member(self):
        """A chunk's ``rom_ids`` include every fetched sibling of its groups, not just
        the emitted representative."""
        # Group "g" has three fetched members (10, 11, 12) but emits one shortcut.
        emitted = [_sd(10, "g")]
        shortcuts_data = [_sd(10, "g"), _sd(11, "g"), _sd(12, "g")]
        chunks = build_unit_chunks(emitted, shortcuts_data, 200)
        assert len(chunks) == 1
        assert sorted(chunks[0].rom_ids) == [10, 11, 12]

    def test_rebind_entry_pulls_representative_group_rows_into_its_chunk(self):
        """A rebind entry (keyed to a vanished sibling, carrying the representative's
        group key) routes the representative's whole group into the rebind's chunk."""
        # chunk_size 1 forces each run into its own chunk. The rebind entry's
        # rom_id (99) is the vanished sibling, absent from shortcuts_data; its
        # sibling_group_key "g" and bind_rom_id point at the surviving group.
        emitted = [_sd(1, "a"), {"rom_id": 99, "sibling_group_key": "g", "bind_rom_id": 10}]
        shortcuts_data = [_sd(1, "a"), _sd(10, "g"), _sd(11, "g"), _sd(12, "g")]
        chunks = build_unit_chunks(emitted, shortcuts_data, 1)
        assert len(chunks) == 2
        assert chunks[0].rom_ids == [1]
        # The representative (bind target 10) and its siblings ride the rebind's chunk.
        assert sorted(chunks[1].rom_ids) == [10, 11, 12]

    def test_keyless_rebind_target_row_lands_in_the_rebind_chunk(self):
        """A KEYLESS rebind entry routes its representative's row into the rebind's
        own chunk — not into chunk 0's leftover.

        A keyless rebind (``sibling_group_key`` None, ``bind_rom_id`` set) has a
        vanished sibling as its ``rom_id`` and the surviving representative as its
        ``bind_rom_id``. Routing by the entry's own ``rom_id`` would leave the
        representative's row in chunk 0's leftover while the entry sits in a later
        chunk — that chunk's ack would then never commit the binding move and the
        rebind would re-fire every sync. This pins the documented invariant "every
        emitted entry's binding target lands in its own chunk's rom_ids" for the
        keyless case too.
        """
        emitted = [_sd(1, None), {"rom_id": 99, "sibling_group_key": None, "bind_rom_id": 10}]
        shortcuts_data = [_sd(1, None), _sd(10, None), _sd(11, None)]
        chunks = build_unit_chunks(emitted, shortcuts_data, 1)
        assert len(chunks) == 2
        # The rebind entry (rom_id 99) is in chunk 1; its bind target (rom 10)
        # must ride the SAME chunk so its ack commits the rebound representative.
        rebind_chunk = next(ci for ci, c in enumerate(chunks) if any(e["rom_id"] == 99 for e in c.emitted))
        assert rebind_chunk == 1
        assert 10 in chunks[rebind_chunk].rom_ids

    def test_groups_with_no_emitted_entry_land_in_first_chunk(self):
        """Fetched groups the collapse emitted nothing for (partial-view grandfather)
        commit with chunk 0, so their rows are never dropped."""
        emitted = [_sd(1, "a")]
        shortcuts_data = [_sd(1, "a"), _sd(50, "z"), _sd(51, "z")]
        chunks = build_unit_chunks(emitted, shortcuts_data, 200)
        assert len(chunks) == 1
        assert sorted(chunks[0].rom_ids) == [1, 50, 51]

    def test_no_emit_leftover_lands_in_chunk_zero_with_multiple_chunks(self):
        """With several chunks, no-emit leftover rows commit with the FIRST chunk."""
        emitted = [_sd(i, None) for i in range(1, 5)]  # 4 singletons → 2 chunks at size 2
        shortcuts_data = [*emitted, _sd(90, "z"), _sd(91, "z")]  # group z emits nothing
        chunks = build_unit_chunks(emitted, shortcuts_data, 2)
        assert len(chunks) == 2
        assert sorted(chunks[0].rom_ids) == [1, 2, 90, 91]
        assert sorted(chunks[1].rom_ids) == [3, 4]

    def test_450_singles_split_into_three_chunks_of_200_200_50(self):
        """The headline case: a >200 unit of singletons emits three chunks."""
        emitted = [_sd(i, f"romm:{i}:1") for i in range(1, 451)]
        chunks = build_unit_chunks(emitted, list(emitted), 200)
        assert [len(c.emitted) for c in chunks] == [200, 200, 50]
        assert [c.offset for c in chunks] == [0, 200, 400]
        # Exact partition across the three chunks.
        assert sorted(rid for c in chunks for rid in c.rom_ids) == list(range(1, 451))


# ── Property-based invariants ────────────────────────────────────────


@st.composite
def _unit(draw: st.DrawFn) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Generate a plausible (emitted, shortcuts_data, chunk_size).

    Groups are laid out in order with globally-unique rom_ids, so emitted stays
    group-clustered (a group's emitted entries are contiguous) exactly as
    ``collapse_sibling_groups`` produces it. Each group emits a subset (possibly
    empty — the no-emit-group case) of its members.
    """
    n_groups = draw(st.integers(min_value=0, max_value=8))
    chunk_size = draw(st.integers(min_value=1, max_value=5))
    next_rid = 1
    emitted: list[dict[str, Any]] = []
    shortcuts_data: list[dict[str, Any]] = []
    for g in range(n_groups):
        key = f"grp:{g}"
        n_members = draw(st.integers(min_value=1, max_value=4))
        member_ids = list(range(next_rid, next_rid + n_members))
        next_rid += n_members
        shortcuts_data.extend(_sd(rid, key) for rid in member_ids)
        n_emit = draw(st.integers(min_value=0, max_value=n_members))
        emitted.extend(_sd(rid, key) for rid in member_ids[:n_emit])
    return emitted, shortcuts_data, chunk_size


@given(unit=_unit())
def test_property_concatenated_emitted_reproduces_input(unit):
    """Concatenating every chunk's emitted reproduces the input emitted, in order."""
    emitted, shortcuts_data, chunk_size = unit
    chunks = build_unit_chunks(emitted, shortcuts_data, chunk_size)
    assert [e for c in chunks for e in c.emitted] == emitted


@given(unit=_unit())
def test_property_rom_ids_are_an_exact_partition(unit):
    """The union of chunk rom_ids is exactly the shortcuts_data rom_ids, no duplicate."""
    emitted, shortcuts_data, chunk_size = unit
    chunks = build_unit_chunks(emitted, shortcuts_data, chunk_size)
    all_rom_ids = [rid for c in chunks for rid in c.rom_ids]
    assert len(all_rom_ids) == len(set(all_rom_ids))  # no duplicate across chunks
    assert set(all_rom_ids) == {sd["rom_id"] for sd in shortcuts_data}


@given(unit=_unit())
def test_property_no_group_split_across_chunks(unit):
    """Every sibling-group key's rows live in a single chunk — never split."""
    emitted, shortcuts_data, chunk_size = unit
    chunks = build_unit_chunks(emitted, shortcuts_data, chunk_size)
    key_by_rid = {sd["rom_id"]: sd["sibling_group_key"] for sd in shortcuts_data}
    key_chunks: dict[str | None, set[int]] = {}
    for ci, chunk in enumerate(chunks):
        for rid in chunk.rom_ids:
            key_chunks.setdefault(key_by_rid[rid], set()).add(ci)
    assert all(len(indices) == 1 for indices in key_chunks.values())


@st.composite
def _unit_with_rebinds(draw: st.DrawFn) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    """Generate a (emitted, shortcuts_data, chunk_size) that includes REBIND lanes.

    Each group is emitted via one of three lanes — ``new`` (one representative),
    ``grandfather`` (a subset of members), or ``rebind`` (a synthetic entry whose
    ``rom_id`` is a VANISHED sibling absent from shortcuts_data, carrying a
    ``bind_rom_id`` pointing at a member representative that IS present). A group's
    key is either a real key (keyed lane) or ``None`` (keyless lane), so the
    generator exercises the keyless-rebind routing branch specifically — the one
    that stranded the representative's row before the fix. Every emitted entry's
    binding target is therefore always a member present in shortcuts_data.
    """
    n_groups = draw(st.integers(min_value=1, max_value=6))
    chunk_size = draw(st.integers(min_value=1, max_value=4))
    next_rid = 1
    vanished_rid = 10_000  # disjoint from member ids so a vanished sibling never collides
    emitted: list[dict[str, Any]] = []
    shortcuts_data: list[dict[str, Any]] = []
    for g in range(n_groups):
        key = f"grp:{g}" if draw(st.booleans()) else None
        n_members = draw(st.integers(min_value=1, max_value=3))
        member_ids = list(range(next_rid, next_rid + n_members))
        next_rid += n_members
        shortcuts_data.extend(_sd(rid, key) for rid in member_ids)
        lane = draw(st.sampled_from(["new", "grandfather", "rebind"]))
        if lane == "new":
            emitted.append(_sd(member_ids[0], key))
        elif lane == "grandfather":
            n_emit = draw(st.integers(min_value=1, max_value=n_members))
            emitted.extend(_sd(rid, key) for rid in member_ids[:n_emit])
        else:  # rebind: a vanished sibling emitted, bind target = a present member
            emitted.append({"rom_id": vanished_rid, "sibling_group_key": key, "bind_rom_id": member_ids[0]})
            vanished_rid += 1
    return emitted, shortcuts_data, chunk_size


@given(unit=_unit_with_rebinds())
def test_property_binding_target_lands_in_own_chunk(unit):
    """Every emitted entry's binding target commits in the entry's OWN chunk.

    The binding target is ``bind_rom_id`` when present (a rebind entry) else the
    entry's own ``rom_id``. It must land in the same chunk's ``rom_ids`` as the
    entry, so that chunk's ack persists the binding. Fails for keyless rebind
    entries before the fix (their target routed to chunk 0's leftover).
    """
    emitted, shortcuts_data, chunk_size = unit
    chunks = build_unit_chunks(emitted, shortcuts_data, chunk_size)
    rid_to_chunk: dict[int, int] = {}
    for ci, chunk in enumerate(chunks):
        for rid in chunk.rom_ids:
            rid_to_chunk[rid] = ci
    for ci, chunk in enumerate(chunks):
        for entry in chunk.emitted:
            target = int(entry.get("bind_rom_id", entry["rom_id"]))
            assert rid_to_chunk.get(target) == ci
