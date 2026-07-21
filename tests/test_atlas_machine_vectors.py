"""Self-conformance of the plugin's save-path kernel against emu-atlas machine vectors.

emu-atlas (https://github.com/danielcopper/emu-atlas) is the config-aware emulator-knowledge
library extracted from this plugin; its ``machines`` vector family is the normative contract for
where a RetroArch / RetroDECK install keeps its saves, and its save-placement expectations were
oracle-derived from this repo's ``domain/save_path.resolve_save_dir`` /
``compute_local_save_target``. This tier drives the plugin's OWN kernel over the same fixture
machines and asserts it still agrees with the published contract where the two overlap.

The overlap is partial by design, so every one of the 16 vectors carries an explicit
``_CHECK_LEVELS`` entry:

- ``full`` — end-to-end placement. The plugin derives the saves root the same way atlas does (from
  ``retrodeck.json``, or the ``~/retrodeck`` fallback), so the final directory + filename strings
  are compared. RetroDECK-flavor ``InSaveDir`` vectors, plus the RetroDECK-first coexistence case.
- ``layout-only`` — only the ``retroarch.cfg`` interpretation overlaps. The plugin has no
  standalone-RetroArch saves-root concept (its saves base always comes from RetroDECK paths), so a
  vector whose placement hangs off a standalone ``savefile_directory`` cannot be checked
  end-to-end; what DOES overlap is the ``SaveLayout`` the plugin derives from the same cfg text —
  the sort flags for an ``InSaveDir`` placement, or the ``ContentDir`` (next-to-ROM) classification.
- ``n/a`` — no overlap. The plugin has no installation-enumeration surface, so atlas's
  "nothing detected" outcome has no plugin equivalent.

The vendored JSON under ``atlas_vectors/machines/`` is the contract's normative artifact and changes
only by a deliberate re-copy from upstream (see ``atlas_vectors/README.md``) — never by editing a
vector to match the kernel. A new upstream vector without a ``_CHECK_LEVELS`` entry fails at
collection rather than silently passing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import pytest

from adapters.retroarch_config import RetroArchConfigAdapter
from adapters.retrodeck_paths import RetroDeckPathsAdapter
from domain.save_layout import ContentDir, InSaveDir, SaveLayout
from domain.save_path import compute_local_save_target, resolve_save_dir

_VECTORS_FILE = Path(__file__).parent / "atlas_vectors" / "machines" / "named-cases.json"

# A fixed ROM stem + file extension used to fill atlas's ``<rom_stem>`` hole on both sides of every
# comparison. The value is irrelevant to the placement math — it only has to be identical in the
# kernel input and the substituted expectation.
_ROM_STEM = "Game"
_ROM_EXT = ".gba"

# Per-vector check level + the reason the level was chosen. Every one of the 16 machine vectors is
# listed; the test fails at collection if a name is missing (a new upstream vector) or stale (a
# removed one), so the applicability split can never silently rot.
_CHECK_LEVELS: dict[str, tuple[str, str]] = {
    # --- full: RetroDECK saves root is the plugin's own source, InSaveDir end-to-end placement ---
    "retrodeck-custom-home-sort-by-content": (
        "full",
        "saves root from retrodeck.json (the plugin's own source); sort-by-content placement.",
    ),
    "retrodeck-fallback-home": (
        "full",
        "empty retrodeck.json -> the plugin's <home>/retrodeck/saves fallback == atlas's fallback root.",
    ),
    "retrodeck-no-cfg-defaults": (
        "full",
        "no retroarch.cfg -> the plugin's default InSaveDir(sort_by_content=True) == atlas's RetroDECK defaults.",
    ),
    "retrodeck-sort-disabled-flat": (
        "full",
        "both sort flags off -> a flat saves root, no content/core subdir.",
    ),
    "retrodeck-sort-by-content-and-core": (
        "full",
        "both sort flags on + core supplied -> saves/<content>/<core>.",
    ),
    "retrodeck-sort-by-core-only": (
        "full",
        "sort-by-core only + core supplied -> saves/<core>.",
    ),
    "retrodeck-content-folder-provided": (
        "full",
        "sort-by-content + rom_dir_name supplied -> saves/<folder>.",
    ),
    "coexistence-retrodeck-and-native": (
        "full",
        "RetroDECK is highest priority in atlas's install order AND the plugin's cfg probe order; "
        "placement is answered by RetroDECK, so the end-to-end path overlaps.",
    ),
    # --- layout-only: only the retroarch.cfg interpretation overlaps ---
    "retrodeck-savefiles-in-content-dir": (
        "layout-only",
        "savefiles_in_content_dir=true -> the plugin returns ContentDir() (save sync unsupported, no "
        "saves dir computed); the overlap is the cfg -> ContentDir classification == atlas's next-to-ROM placement.",
    ),
    "standalone-flatpak-savefile-directory-set": (
        "layout-only",
        "saves root is the standalone savefile_directory, which the plugin does not model (its saves base "
        "is always RetroDECK's); only the sort_by_content flag derived from the same cfg overlaps.",
    ),
    "standalone-flatpak-savefile-directory-absent": (
        "layout-only",
        "saves root is an unfilled <savefile_directory> hole the plugin does not model; only sort_by_content overlaps.",
    ),
    "standalone-flatpak-content-dir-mode": (
        "layout-only",
        "standalone content-dir mode -> the plugin returns ContentDir(); the cfg -> ContentDir classification "
        "overlaps atlas's next-to-ROM placement, flavor-independent.",
    ),
    "native-savefile-directory-set-flat": (
        "layout-only",
        "native savefile_directory saves root the plugin does not model; only the both-flags-off -> flat "
        "cfg interpretation overlaps.",
    ),
    "native-savefile-directory-default-is-hole": (
        "layout-only",
        "savefile_directory='default' sentinel is atlas-specific (the plugin does not parse savefile_directory); "
        "only sort_by_content overlaps.",
    ),
    "coexistence-standalone-and-native": (
        "layout-only",
        "highest-priority install is standalone (no RetroDECK); saves root the plugin does not model; only the "
        "both-flags-off -> flat cfg interpretation overlaps.",
    ),
    # --- n/a: no overlap ---
    "nothing-installed": (
        "n/a",
        "the plugin has no installation-enumeration surface -- RetroDeckPathsAdapter always returns fallback "
        "paths and RetroArchConfigAdapter always returns a default layout, so atlas's 'nothing detected' "
        "outcome has no plugin equivalent.",
    ),
}


def _load_vectors() -> tuple[list[dict[str, Any]], list[str]]:
    """Flatten the machine-vector file into parametrize argvalues + ids.

    Returns ``(vectors, ids)`` where each id is the vector's ``name`` so a failure names the
    offending vector directly.
    """
    data: dict[str, Any] = json.loads(_VECTORS_FILE.read_text(encoding="utf-8"))
    vectors: list[dict[str, Any]] = data["vectors"]
    return vectors, [v["name"] for v in vectors]


_VECTORS, _IDS = _load_vectors()

# An empty load is a vendoring regression (file deleted or moved), not a valid "nothing to test"
# state — fail loudly at collection rather than silently pass.
assert _VECTORS, f"no atlas machine vectors loaded from {_VECTORS_FILE}"

# The allowlist and the vendored vectors must name exactly the same set — a new upstream vector
# (missing entry) or a removed one (stale entry) both fail here, forcing the applicability split to
# be re-examined in the same diff that changes the vectors.
_VECTOR_NAMES = {v["name"] for v in _VECTORS}
assert set(_CHECK_LEVELS) == _VECTOR_NAMES, (
    "atlas machine-vector allowlist drift: "
    f"vectors-only={sorted(_VECTOR_NAMES - set(_CHECK_LEVELS))}, "
    f"allowlist-only={sorted(set(_CHECK_LEVELS) - _VECTOR_NAMES)}"
)


def _rewrite_home(text: str, home: str, fake_home: str) -> str:
    """Rewrite the vector's ``home`` prefix onto the tmp-rooted fake home.

    The machine vectors are written against ``/home/deck``; the test materializes them under a
    ``tmp_path`` fake home. Non-home absolute roots (an SD-card ``/mnt/sd/...`` path, a ``~`` value)
    are left untouched — they are deliberately outside home.
    """
    return text.replace(home, fake_home)


def _materialize(files: dict[str, str], home: str, fake_home: str) -> None:
    """Write the vector's ``{path: content}`` file tree under the fake home."""
    for raw_path, raw_content in files.items():
        path = _rewrite_home(raw_path, home, fake_home)
        content = _rewrite_home(raw_content, home, fake_home)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        Path(path).write_text(content, encoding="utf-8")


def _make_adapters(fake_home: str) -> tuple[RetroDeckPathsAdapter, SaveLayout]:
    """Build the plugin adapters against the fake home and read the derived save layout."""
    logger = logging.getLogger("atlas-conformance")
    retrodeck_paths = RetroDeckPathsAdapter(user_home=fake_home, logger=logger)
    layout = RetroArchConfigAdapter(user_home=fake_home, logger=logger).get_save_layout()
    return retrodeck_paths, layout


def _dir_components(dir_template: str) -> list[str]:
    """Split a placement dir into path components for hole detection."""
    return [c for c in dir_template.replace(os.sep, "/").split("/") if c]


def _expected_filename(placement: dict[str, Any]) -> str:
    """Atlas's expected filename with the ``<rom_stem>`` hole filled by the chosen stem."""
    return placement["filename"].replace("<rom_stem>", _ROM_STEM)


def _check_full(
    vector: dict[str, Any], retrodeck_paths: RetroDeckPathsAdapter, layout: SaveLayout, home: str, fake_home: str
) -> None:
    """End-to-end: the plugin's resolved save dir + filename must equal atlas's placement.

    The ``<content_dir>`` hole is filled by ``rom_dir_name`` when the query supplies it (exercising
    the vector's own content folder), otherwise by the system slug; the same value is substituted
    into atlas's template, so the root and the sort semantics — not the hole fill — carry the
    comparison.
    """
    assert isinstance(layout, InSaveDir), f"full vector {vector['name']!r} expected an InSaveDir layout, got {layout!r}"
    query = vector["input"]["query"]
    system = query["system"]
    placement = vector["expected"]["save_placement"]

    content_folder = query.get("rom_dir_name") or system
    core_name = query.get("core")

    saves_base = retrodeck_paths.saves_path()
    roms_base = retrodeck_paths.roms_path()
    rom_path = os.path.join(roms_base, content_folder, _ROM_STEM + _ROM_EXT)

    actual_dir = resolve_save_dir(
        rom_path,
        saves_base,
        system,
        roms_base=roms_base,
        sort_by_content=layout.sort_by_content,
        sort_by_core=layout.sort_by_core,
        core_name=core_name,
    )

    expected_dir = placement["dir"]
    expected_dir = expected_dir.replace("<content_dir>", content_folder)
    expected_dir = expected_dir.replace("<rom_stem>", _ROM_STEM)
    if core_name is not None:
        expected_dir = expected_dir.replace("<core>", core_name)
    expected_dir = _rewrite_home(expected_dir, home, fake_home)

    assert actual_dir == expected_dir, (
        f"placement dir drift on {vector['name']!r}: kernel {actual_dir!r} != atlas {expected_dir!r}"
    )

    actual_filename = compute_local_save_target({}, _ROM_STEM).filename
    assert actual_filename == _expected_filename(placement), (
        f"filename drift on {vector['name']!r}: kernel {actual_filename!r} != atlas {_expected_filename(placement)!r}"
    )


def _check_layout_only(vector: dict[str, Any], layout: SaveLayout) -> None:
    """Overlap is the cfg interpretation only: the derived SaveLayout must match what the placement implies."""
    placement = vector["expected"]["save_placement"]
    dir_template = placement["dir"]

    if dir_template == "<content_dir>":
        # Next-to-ROM: the whole dir IS the content hole, no saves root prefix.
        assert isinstance(layout, ContentDir), (
            f"content-dir vector {vector['name']!r} expected ContentDir(), got {layout!r}"
        )
        return

    components = _dir_components(dir_template)
    expected_sort_by_content = "<content_dir>" in components
    expected_sort_by_core = "<core>" in components
    assert isinstance(layout, InSaveDir), (
        f"layout-only vector {vector['name']!r} expected an InSaveDir layout, got {layout!r}"
    )
    assert layout.sort_by_content == expected_sort_by_content, (
        f"sort_by_content drift on {vector['name']!r}: kernel {layout.sort_by_content} != "
        f"placement-implied {expected_sort_by_content} (dir {dir_template!r})"
    )
    assert layout.sort_by_core == expected_sort_by_core, (
        f"sort_by_core drift on {vector['name']!r}: kernel {layout.sort_by_core} != "
        f"placement-implied {expected_sort_by_core} (dir {dir_template!r})"
    )
    # The filename math is pure and flavor-independent, so it overlaps even when the dir does not.
    actual_filename = compute_local_save_target({}, _ROM_STEM).filename
    assert actual_filename == _expected_filename(placement), (
        f"filename drift on {vector['name']!r}: kernel {actual_filename!r} != atlas {_expected_filename(placement)!r}"
    )


def _check_na(vector: dict[str, Any]) -> None:
    """No overlap: guard that the vector stays in its non-checkable shape.

    If a future upstream revision gives this vector installations or a placement, the classification
    is wrong and this fails, forcing the applicability split to be reconsidered.
    """
    expected = vector["expected"]
    assert expected["installations"] == [], (
        f"n/a vector {vector['name']!r} now reports installations {expected['installations']!r} -- reclassify"
    )
    assert "save_placement" not in expected, f"n/a vector {vector['name']!r} now carries a save_placement -- reclassify"


@pytest.mark.parametrize("vector", _VECTORS, ids=_IDS)
def test_kernel_conforms_to_atlas_machine_vector(vector: dict[str, Any], tmp_path: Path) -> None:
    level, _reason = _CHECK_LEVELS[vector["name"]]
    home = vector["input"]["home"]
    fake_home = str(tmp_path)
    _materialize(vector["input"]["files"], home, fake_home)
    retrodeck_paths, layout = _make_adapters(fake_home)

    if level == "full":
        _check_full(vector, retrodeck_paths, layout, home, fake_home)
    elif level == "layout-only":
        _check_layout_only(vector, layout)
    elif level == "n/a":
        _check_na(vector)
    else:  # pragma: no cover - guarded by the allowlist drift assertion at collection
        pytest.fail(f"unknown check level {level!r} for {vector['name']!r}")
