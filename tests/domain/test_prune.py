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


def test_bundle_id_accepts_only_trusted_components():
    assert recovery_bundle_id("20260724T120000Z", 7, "abc-123") == "20260724T120000Z_7_abc-123"
    with pytest.raises(ValueError):
        recovery_bundle_id("now", 0, "abc")
    with pytest.raises(ValueError):
        recovery_bundle_id("now", 1, "../escape")


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
