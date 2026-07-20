"""Tests for the skip's countable-row kernel (``domain/fetch_generation.py``)."""

from __future__ import annotations

from domain.fetch_generation import count_rows_for_skip
from domain.rom import Rom


def _row(rom_id: int, fetch_id: str | None) -> Rom:
    rom = Rom.synced(
        rom_id=rom_id,
        platform_slug="dc",
        name=f"rom-{rom_id}",
        fs_name=f"rom-{rom_id}.gdi",
        shortcut_app_id=None,
        synced_at="2026-07-20T06:27:12",
    )
    if fetch_id is not None:
        rom.record_fetch_generation(fetch_id)
    return rom


class TestCountRowsForSkip:
    def test_counts_only_rows_of_the_named_generation(self):
        # The live #1504 shape: 4375/4376 superseded, 25135/25136 current.
        rows = [
            _row(4375, "run-old"),
            _row(4376, "run-old"),
            _row(25135, "run-new"),
            _row(25136, "run-new"),
        ]
        assert count_rows_for_skip(rows, "run-new") == 2

    def test_superseded_rows_stop_blocking_the_server_count_match(self):
        rows = [_row(4375, "run-old"), _row(25135, "run-new"), _row(25136, "run-new")]
        server_rom_count = 2
        assert count_rows_for_skip(rows, "run-new") == server_rom_count

    def test_counts_bound_and_unbound_rows_alike(self):
        # Group-aware sync persists every sibling (ADR-0021); only the generation
        # decides, never the binding.
        bound = _row(25135, "run-new")
        bound.bind_shortcut(4185303886)
        assert count_rows_for_skip([bound, _row(25136, "run-new")], "run-new") == 2

    def test_row_with_no_generation_never_matches(self):
        assert count_rows_for_skip([_row(4375, None), _row(25135, "run-new")], "run-new") == 1

    def test_unknown_stamp_generation_counts_every_row(self):
        # A stamp written before the generation contract cannot say what its fetch
        # saw, so the pre-#1504 behavior stands until the next fetch re-stamps.
        rows = [_row(4375, None), _row(25135, "run-new")]
        assert count_rows_for_skip(rows, None) == 2

    def test_empty_stamp_generation_counts_every_row(self):
        assert count_rows_for_skip([_row(4375, None)], "") == 1

    def test_no_rows_counts_zero(self):
        assert count_rows_for_skip([], "run-new") == 0
        assert count_rows_for_skip([], None) == 0

    def test_a_platform_with_no_superseded_rows_counts_all_of_them(self):
        rows = [_row(25135, "run-new"), _row(25136, "run-new")]
        assert count_rows_for_skip(rows, "run-new") == len(rows)
