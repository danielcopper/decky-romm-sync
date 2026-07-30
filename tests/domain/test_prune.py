from __future__ import annotations

import pytest

from domain.prune import group_rows, recovery_bundle_id, sanitize_package_name, selected_prune_ids
from domain.rom import Rom
from domain.version_metadata import VersionMetadata


def _rom(rom_id: int, group: str | None) -> Rom:
    return Rom.synced(
        rom_id=rom_id,
        platform_slug="dc",
        name=str(rom_id),
        fs_name=f"{rom_id}.gdi",
        shortcut_app_id=None,
        synced_at="now",
        version=VersionMetadata(sibling_group_key=group),
    )


def test_package_name_sanitization_and_fallback():
    assert sanitize_package_name("decky romm/../sync") == "decky-romm-..-sync"
    assert sanitize_package_name("dëcky") == "d-cky"
    assert sanitize_package_name("aéb") == "a-b"
    assert sanitize_package_name("///") == "decky-plugin"
    assert sanitize_package_name(None) == "decky-plugin"


def test_bundle_id_leads_with_the_game_and_stays_a_safe_path_component():
    assert (
        recovery_bundle_id("Shenmue II (Europe)", "2026-07-24", "a436b01a") == "Shenmue-II-Europe_2026-07-24_a436b01a"
    )
    # A name that sanitizes away still yields a usable component.
    assert recovery_bundle_id("///", "2026-07-24", "abcd") == "game_2026-07-24_abcd"
    assert recovery_bundle_id(None, "2026-07-24", "abcd") == "game_2026-07-24_abcd"


def test_bundle_id_refuses_untrusted_date_and_id_components():
    with pytest.raises(ValueError):
        recovery_bundle_id("Game", "now", "abcd")
    with pytest.raises(ValueError):
        recovery_bundle_id("Game", "2026-07-24", "../escape")
    with pytest.raises(ValueError):
        recovery_bundle_id("Game", "2026-07-24", "no")


def test_bundle_name_cannot_escape_or_hide_its_directory():
    # Traversal, separators and leading dots are the shapes that would turn a
    # game's own name into a path escape or a hidden directory.
    assert "/" not in recovery_bundle_id("../../etc/passwd", "2026-07-24", "abcd")
    assert recovery_bundle_id("../../etc/passwd", "2026-07-24", "abcd").startswith("etc-passwd")
    assert recovery_bundle_id("...hidden", "2026-07-24", "abcd").startswith("hidden")
    assert len(recovery_bundle_id("N" * 500, "2026-07-24", "abcd")) < 100


def test_null_group_keys_are_independent_singletons():
    groups = group_rows([_rom(3, None), _rom(2, "same"), _rom(1, None), _rom(4, "same")])
    assert [[row.rom_id for row in group] for group in groups] == [[1], [2, 4], [3]]


def test_selected_ids_truth_table():
    assert selected_prune_ids(
        group_ids=[1, 2],
        candidate_ids={1},
        vanished_ids={1},
        live_ids={2},
        remove_rows=True,
        remove_fully_vanished=False,
    ) == {1}
    assert selected_prune_ids(
        group_ids=[1, 2],
        candidate_ids={1},
        vanished_ids={1, 2},
        live_ids=set(),
        remove_rows=False,
        remove_fully_vanished=True,
    ) == {1, 2}
    assert (
        selected_prune_ids(
            group_ids=[1],
            candidate_ids={1},
            vanished_ids={1},
            live_ids=set(),
            remove_rows=True,
            remove_fully_vanished=False,
        )
        == set()
    )
