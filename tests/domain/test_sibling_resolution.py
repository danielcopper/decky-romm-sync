"""Tests for domain.sibling_resolution — the sibling-group representative chain (ADR-0021 §3)."""

from __future__ import annotations

import pytest

from domain.sibling_resolution import resolve_group_representative


def _m(rom_id, *, fs_name_no_ext="", is_main_sibling=False):
    """A group member dict as build_shortcuts_data shapes it for the resolver."""
    return {"rom_id": rom_id, "fs_name_no_ext": fs_name_no_ext, "is_main_sibling": is_main_sibling}


class TestResolveGroupRepresentative:
    def test_installed_wins_over_binding_default_and_alphabetical(self):
        members = [
            _m(1, fs_name_no_ext="a_first", is_main_sibling=True),  # default + alphabetically first
            _m(2, fs_name_no_ext="z_last"),  # only this one is installed
        ]
        # rom 2 is installed AND bound; rom 1 is the default and alphabetically first.
        assert resolve_group_representative(members, installed_rom_ids={2}, bound_rom_ids={2}) == 2

    def test_multiple_installed_break_by_alphabetical_then_rom_id(self):
        members = [
            _m(5, fs_name_no_ext="beta"),
            _m(3, fs_name_no_ext="alpha"),  # alphabetically first among installed
            _m(9, fs_name_no_ext="alpha"),
        ]
        # roms 3 and 9 tie on fs_name_no_ext="alpha" → lower rom_id (3) wins.
        assert resolve_group_representative(members, installed_rom_ids={3, 5, 9}, bound_rom_ids=set()) == 3

    def test_existing_binding_wins_when_none_installed(self):
        members = [
            _m(1, fs_name_no_ext="a_first", is_main_sibling=True),
            _m(2, fs_name_no_ext="z_last"),
        ]
        # Nothing installed; rom 2 carries the existing binding → it wins over the default.
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids={2}) == 2

    def test_default_wins_when_none_installed_or_bound(self):
        members = [
            _m(1, fs_name_no_ext="z_last", is_main_sibling=True),  # RomM default
            _m(2, fs_name_no_ext="a_first"),  # alphabetically first but not default
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 1

    def test_alphabetical_fallback_when_no_installed_bound_or_default(self):
        members = [
            _m(1, fs_name_no_ext="Zelda"),
            _m(2, fs_name_no_ext="alpha"),  # lower-cased "alpha" < "zelda"
            _m(3, fs_name_no_ext="Mario"),
        ]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 2

    def test_rom_id_is_final_tie_break_on_equal_names(self):
        members = [_m(7, fs_name_no_ext="Game"), _m(3, fs_name_no_ext="Game")]
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 3

    def test_multiple_defaults_break_alphabetically(self):
        members = [
            _m(1, fs_name_no_ext="z", is_main_sibling=True),
            _m(2, fs_name_no_ext="a", is_main_sibling=True),
        ]
        # Both are RomM defaults → alphabetical fs_name_no_ext decides.
        assert resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set()) == 2

    def test_solo_group_returns_its_only_member(self):
        assert resolve_group_representative([_m(42)], installed_rom_ids=set(), bound_rom_ids=set()) == 42

    def test_empty_members_raises_value_error(self):
        with pytest.raises(ValueError, match="empty sibling group"):
            resolve_group_representative([], installed_rom_ids=set(), bound_rom_ids=set())

    def test_result_ignores_member_order(self):
        members = [
            _m(1, fs_name_no_ext="z_last"),
            _m(2, fs_name_no_ext="a_first", is_main_sibling=True),
            _m(3, fs_name_no_ext="m_mid"),
        ]
        forward = resolve_group_representative(members, installed_rom_ids=set(), bound_rom_ids=set())
        reversed_ = resolve_group_representative(list(reversed(members)), installed_rom_ids=set(), bound_rom_ids=set())
        assert forward == reversed_ == 2
