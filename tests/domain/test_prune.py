from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from domain.prune import (
    _READABLE_KINDS,
    group_rows,
    recovery_bundle_id,
    render_bundle_readme,
    sanitize_package_name,
    selected_prune_ids,
)
from domain.rom import Rom
from domain.version_metadata import VersionMetadata

if TYPE_CHECKING:
    from domain.prune import BundleReadmeContext

# Every kind a producer stamps onto a RecoveryArtifact, by producer site:
# PruneSaveSupport (services/saves/prune_support.py) emits current_save and
# save_backup, RecoveryCoordinator (services/prune/recovery.py) installed_rom,
# SteamRecoveryAdapter (adapters/steam_recovery.py) steam_grid and steam_input,
# PruneArtifactAdapter (adapters/prune_artifacts.py) the three cache kinds.
_PRODUCED_KINDS = {
    "current_save",
    "save_backup",
    "installed_rom",
    "steam_grid",
    "steam_input",
    "cover_cache",
    "cover_validator",
    "sgdb_cache",
}


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


def _readme_context() -> BundleReadmeContext:
    return {
        "bundle_id": "TestGame_2026-07-24_abc123",
        "created_at": "2026-07-24T12:00:00+00:00",
        "games": [
            {
                "rom_id": 7,
                "name": "Test Game",
                "fs_name": "Test Game.chd",
                "platform_slug": "dc",
                "role": "removed by this cleanup",
            }
        ],
        "playtime_lines": ["Test Game (ROM 7): 894 seconds — 0h 14m 54s"],
    }


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


def test_readable_kinds_match_the_kinds_the_producers_emit():
    # A produced kind with no entry renders as its raw slug; an entry nothing
    # produces outlived its producer. Both drift silently, because the README is
    # only ever read months later by a person restoring by hand.
    assert set(_READABLE_KINDS) == _PRODUCED_KINDS


def test_readme_names_every_artifact_kind_in_plain_words():
    records = [
        {"destination": f"files/{index:06d}", "source_path": f"/sources/file{index}", "size": 1024, "kind": kind}
        for index, kind in enumerate(sorted(_PRODUCED_KINDS), start=1)
    ]

    readme = render_bundle_readme(_readme_context(), records)

    for kind in sorted(_PRODUCED_KINDS):
        assert _READABLE_KINDS[kind] in readme
        assert kind not in readme


def test_null_group_keys_are_independent_singletons():
    groups = group_rows([_rom(3, None), _rom(2, "same"), _rom(1, None), _rom(4, "same")])
    assert [[row.rom_id for row in group] for group in groups] == [[1], [2, 4], [3]]


@pytest.mark.parametrize("remove_fully_vanished", [False, True])
def test_partially_live_group_ignores_the_whole_game_option(remove_fully_vanished):
    """The whole-game branch is unreachable while any member is still live.

    F6 made remove_fully_vanished the default, so this is now the ordinary
    path for a bound vanished row beside a live sibling; the option must not
    change what a partially-live group does (#1570 F17).
    """
    assert selected_prune_ids(
        group_ids=[4375, 25135],
        candidate_ids={4375},
        vanished_ids={4375},
        live_ids={25135},
        remove_rows=True,
        remove_fully_vanished=remove_fully_vanished,
    ) == {4375}


@pytest.mark.parametrize("remove_fully_vanished", [False, True])
def test_partially_live_group_removes_nothing_without_row_removal(remove_fully_vanished):
    # Turning the whole-game option on must not resurrect row removal that the
    # user switched off.
    assert (
        selected_prune_ids(
            group_ids=[4375, 25135],
            candidate_ids={4375},
            vanished_ids={4375},
            live_ids={25135},
            remove_rows=False,
            remove_fully_vanished=remove_fully_vanished,
        )
        == set()
    )


def test_fully_vanished_group_needs_the_whole_game_option():
    for remove_rows in (False, True):
        assert (
            selected_prune_ids(
                group_ids=[1, 2],
                candidate_ids={1},
                vanished_ids={1, 2},
                live_ids=set(),
                remove_rows=remove_rows,
                remove_fully_vanished=False,
            )
            == set()
        )


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
