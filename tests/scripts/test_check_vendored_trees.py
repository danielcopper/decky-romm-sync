"""Tests for ``scripts/check_vendored_trees.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). ``collect_discrepancies``
takes the vendor directory explicitly, so most cases lay out a small synthetic
``_vendor/`` under ``tmp_path`` and the real comparison logic runs against it.
The synthetic directory holds BOTH shapes the gate has to serve — a wheel copy
whose licence sits beside the tree and whose manifest is upstream's own, and a
patched copy whose licence sits inside the tree and whose manifest we generated
— because the gate discovers packages rather than being told about them, and a
one-package fixture cannot show that one tree's drift is reported against that
tree alone. The parser cases need no tree at all, and two cases deliberately use
the real one.

Coverage centres on the four ways a vendored copy stops being what its manifest
pins — a file tampered with, one deleted, one added, and a licence that no
longer matches — because a checksum gate that misses any of them is decorative.
The deleted and added cases are the reason this gate exists at all: ``sha256sum
-c --ignore-missing`` exits 0 on both. A fifth way is a copy that pins nothing:
a package directory with no manifest is a failure, since otherwise vendoring a
package and forgetting its manifest leaves an unguarded tree beside a guarded
one and everything stays green. A sixth is a licence the manifest cannot pin at
all, which is what regenerating a wheel's manifest from its own tree produces.
The manifest's own refusals are pinned too, since each would otherwise drop a
check silently: the three the parser makes (unparseable line, duplicate entry,
an ambiguous licence entry) and the two that happen before it ever runs (bytes
that do not decode, a file that cannot be opened). What the sweep does with
one is pinned at the clause that catches it: the refusal comes back as one
package's finding rather than ending the run, since a raise out of the loop
would take every other tree's drift with it. Two tests run against the REAL
vendored directory — one over ``collect_discrepancies`` and one over ``main``
— so the copies in this repository are verified by ``mise run test`` and not
only by ``mise run lint``.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_vendored_trees.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_vendored_trees", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()

_LICENCE_TEXT = "MIT License\n\nCopyright (c) 2026 danielcopper\n"

_ATLAS_MANIFEST = "atlas.SHA256SUMS"
_ATLAS_LICENCE = "atlas.LICENSE"
_VDF_MANIFEST = "vdf.SHA256SUMS"

# A wheel copy: enough shape to exercise the walk (a nested data file, a
# non-Python marker file) without standing in for the real package. Its licence
# is vendored as a sibling, so the tree stays exactly equal to the manifest.
_WHEEL_TREE = {
    "atlas/__init__.py": '__version__ = "0.5.0"\n',
    "atlas/data/system_ids.json": '{"snes": 4}\n',
    "atlas/py.typed": "",
}

# A patched copy: the licence lives INSIDE the tree, so the manifest carries it
# like any other file and there is no sibling licence to check.
_PATCHED_TREE = {
    "vdf/__init__.py": "from .vdict import VDFDict\n",
    "vdf/LICENSE": _LICENCE_TEXT,
    "vdf/vdict.py": "class VDFDict(dict):\n    pass\n",
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _wheel_manifest_text(tree: dict[str, str], licence: str = _LICENCE_TEXT) -> str:
    """A manifest in upstream's shape: release artifacts, the tree, the dist-info.

    The non-tree entries are deliberate — they cover files that are NOT
    vendored, so a gate that required every manifest entry on disk would fail
    on a correct copy.
    """
    lines = [
        f"{_digest('tarball')}  emu-atlas-v0.5.0-x86_64-linux.tar.gz",
        f"{_digest('wheel')}  emu_atlas-0.5.0-py3-none-any.whl",
    ]
    lines += [f"{_digest(text)}  {path}" for path, text in sorted(tree.items())]
    lines += [
        f"{_digest('metadata')}  emu_atlas-0.5.0.dist-info/METADATA",
        f"{_digest(licence)}  emu_atlas-0.5.0.dist-info/licenses/LICENSE",
    ]
    return "\n".join(lines) + "\n"


def _generated_manifest_text(tree: dict[str, str]) -> str:
    """A manifest in the shape ``sha256sum`` writes over the tree itself — nothing else in it."""
    return "\n".join(f"{_digest(text)}  {path}" for path, text in sorted(tree.items())) + "\n"


@pytest.fixture
def vendor_dir(tmp_path: Path) -> Path:
    """A synthetic ``_vendor/`` whose two copies both match their manifests exactly."""
    root = tmp_path / "_vendor"
    for relative, text in (_WHEEL_TREE | _PATCHED_TREE).items():
        _write(root / relative, text)
    _write(root / _ATLAS_LICENCE, _LICENCE_TEXT)
    _write(root / _ATLAS_MANIFEST, _wheel_manifest_text(_WHEEL_TREE))
    _write(root / _VDF_MANIFEST, _generated_manifest_text(_PATCHED_TREE))
    _write(root / "__init__.py", "")
    _write(root / "README.md", "# Vendored copies\n")
    return root


class TestCleanCopy:
    """Copies that are exactly what their manifests pin must pass — and say nothing else."""

    def test_every_matching_copy_has_no_discrepancies(self, vendor_dir: Path) -> None:
        assert check.collect_discrepancies(vendor_dir) == []

    def test_both_trees_are_discovered(self, vendor_dir: Path) -> None:
        """Discovery is per manifest — neither tree is named in the script."""
        assert check.package_dirs(vendor_dir) == {"atlas", "vdf"}
        assert sorted(check.manifest_paths(vendor_dir)) == ["atlas", "vdf"]

    def test_top_level_non_tree_entries_are_not_packages(self, vendor_dir: Path) -> None:
        """``__init__.py``, the README, the licences and the manifests pin nothing."""
        assert check.collect_discrepancies(vendor_dir) == []
        assert "README.md" not in check.package_dirs(vendor_dir)

    def test_pycache_is_ignored(self, vendor_dir: Path) -> None:
        """Importing a tree writes bytecode that was never in any manifest."""
        _write(vendor_dir / "atlas" / "__pycache__" / "__init__.cpython-311.pyc", "not really bytecode")
        _write(vendor_dir / "__pycache__" / "__init__.cpython-311.pyc", "not really bytecode")
        assert check.collect_discrepancies(vendor_dir) == []

    def test_pycache_named_in_a_manifest_is_ignored_too(self, vendor_dir: Path) -> None:
        """Both sides are filtered, so the report cannot claim a file is missing while it is there.

        Filtering only the filesystem side leaves the manifest entry unmatched
        and reports ``missing from the vendored tree`` over bytecode sitting
        right where the line names it — a red gate whose message is false.
        """
        bytecode = "atlas/__pycache__/__init__.cpython-311.pyc"
        _write(vendor_dir / bytecode, "not really bytecode")
        text = _wheel_manifest_text(_WHEEL_TREE) + f"{_digest('not really bytecode')}  {bytecode}\n"
        _write(vendor_dir / _ATLAS_MANIFEST, text)
        assert check.collect_discrepancies(vendor_dir) == []

    def test_the_real_vendored_directory_matches_its_manifests(self) -> None:
        """The gate run against the copies this repository actually ships."""
        assert check.collect_discrepancies(check.VENDOR_DIR) == []


class TestManifestPerTree:
    """A tree pinned by nothing is the hole this gate closes — vendoring is not enough."""

    def test_package_without_a_manifest_fails(self, vendor_dir: Path) -> None:
        _write(vendor_dir / "zstd" / "__init__.py", "# vendored, pinned by nothing\n")
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert len(discrepancies) == 1
        assert discrepancies[0].startswith("zstd/: no zstd.SHA256SUMS — the tree is pinned by nothing.")
        assert "py_modules/_vendor/README.md" in discrepancies[0]

    def test_a_manifest_without_its_tree_reports_every_file(self, vendor_dir: Path) -> None:
        """A dropped tree must report, not pass because there is nothing left to compare."""
        for relative in _PATCHED_TREE:
            (vendor_dir / relative).unlink()
        (vendor_dir / "vdf").rmdir()
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert len(discrepancies) == len(_PATCHED_TREE)
        assert all("missing from the vendored tree" in line for line in discrepancies)


class TestDrift:
    """The ways a copy's file set stops being what its manifest pins.

    Tampered, deleted, added — and the shapes those take at scale. The fourth
    way, a licence that no longer matches, lives in ``TestLicence``.
    """

    def test_tampered_file_fails(self, vendor_dir: Path) -> None:
        _write(vendor_dir / "atlas" / "__init__.py", '__version__ = "0.5.0"  # local fix\n')
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert len(discrepancies) == 1
        assert discrepancies[0].startswith("atlas/__init__.py: digest ")

    def test_tampered_file_in_the_patched_copy_fails(self, vendor_dir: Path) -> None:
        """A generated manifest pins its tree exactly as hard as an upstream one does."""
        _write(vendor_dir / "vdf" / "vdict.py", "class VDFDict(dict):\n    pass  # our fix\n")
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert len(discrepancies) == 1
        assert discrepancies[0].startswith("vdf/vdict.py: digest ")

    def test_deleted_file_fails(self, vendor_dir: Path) -> None:
        """``sha256sum -c --ignore-missing`` exits 0 here — the whole point of the gate."""
        (vendor_dir / "atlas" / "data" / "system_ids.json").unlink()
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas/data/system_ids.json: in the manifest but missing from the vendored tree"
        ]

    def test_added_file_fails(self, vendor_dir: Path) -> None:
        _write(vendor_dir / "atlas" / "patches.py", "# ours\n")
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas/patches.py: in the vendored tree but not in the manifest"
        ]

    def test_added_nested_file_fails(self, vendor_dir: Path) -> None:
        """The walk is recursive — an extra file one level down must not hide."""
        _write(vendor_dir / "atlas" / "data" / "extra.json", "{}\n")
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas/data/extra.json: in the vendored tree but not in the manifest"
        ]

    def test_symlink_standing_in_for_a_file_fails(self, vendor_dir: Path, tmp_path: Path) -> None:
        """Right content, wrong kind of thing — a vendored tree ships no symlinks.

        Without the guard in ``vendored_tree_files`` this passes: ``is_file()``
        follows the link, the digest is read through to a target holding the
        manifest's own bytes, and nothing is reported.
        """
        target = tmp_path / "outside.py"
        target.write_text(_WHEEL_TREE["atlas/__init__.py"], encoding="utf-8")
        victim = vendor_dir / "atlas" / "__init__.py"
        victim.unlink()
        victim.symlink_to(target)
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas/__init__.py: in the manifest but missing from the vendored tree"
        ]

    def test_a_name_merely_containing_pycache_is_still_drift(self, vendor_dir: Path) -> None:
        """``__pycache__`` is filtered as a whole path SEGMENT, never as a substring.

        Written as ``PYCACHE in path`` the filter passes every other test in
        this module, because the module's two ``__pycache__`` cases both use
        paths where the segment and substring readings agree — and it then
        hides any added file from BOTH sides of the comparison, which is the
        one thing the filter must never do. Naming a file
        ``__pycache__notes.txt``, or dropping it under a directory called
        ``my__pycache__helper/``, would be enough to smuggle it in.
        """
        _write(vendor_dir / "atlas" / "__pycache__notes.txt", "# ours\n")
        _write(vendor_dir / "atlas" / "my__pycache__helper" / "patch.py", "# ours\n")
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas/__pycache__notes.txt: in the vendored tree but not in the manifest",
            "atlas/my__pycache__helper/patch.py: in the vendored tree but not in the manifest",
        ]

    def test_drift_in_one_tree_leaves_the_other_alone(self, vendor_dir: Path) -> None:
        """Each tree is compared against its own manifest, so a report names one of them."""
        _write(vendor_dir / "atlas" / "extra.py", "")
        (vendor_dir / "vdf" / "vdict.py").unlink()
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas/extra.py: in the vendored tree but not in the manifest",
            "vdf/vdict.py: in the manifest but missing from the vendored tree",
        ]

    def test_every_drifted_file_is_reported(self, vendor_dir: Path) -> None:
        """One line per discrepancy — a first failure must not mask the rest."""
        _write(vendor_dir / "atlas" / "__init__.py", "tampered\n")
        (vendor_dir / "atlas" / "py.typed").unlink()
        _write(vendor_dir / "atlas" / "extra.py", "")
        assert len(check.collect_discrepancies(vendor_dir)) == 3


class TestLicence:
    """A licence vendored beside its tree needs its own check — the tree digests miss it."""

    def test_missing_licence_fails(self, vendor_dir: Path) -> None:
        (vendor_dir / _ATLAS_LICENCE).unlink()
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas.LICENSE: missing — upstream's licence, copied from emu_atlas-0.5.0.dist-info/licenses/LICENSE"
        ]

    def test_tampered_licence_fails(self, vendor_dir: Path) -> None:
        _write(vendor_dir / _ATLAS_LICENCE, _LICENCE_TEXT.replace("MIT", "GPL"))
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert len(discrepancies) == 1
        assert discrepancies[0].startswith("atlas.LICENSE: digest ")

    def test_flat_dist_info_licence_is_recognised(self, vendor_dir: Path) -> None:
        """Pre-PEP-639 wheels write ``dist-info/LICENSE`` — an unread shape checks one thing less."""
        text = _wheel_manifest_text(_WHEEL_TREE).replace("dist-info/licenses/LICENSE", "dist-info/LICENSE")
        _write(vendor_dir / _ATLAS_MANIFEST, text)
        _write(vendor_dir / _ATLAS_LICENCE, _LICENCE_TEXT.replace("MIT", "GPL"))
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert len(discrepancies) == 1
        assert "emu_atlas-0.5.0.dist-info/LICENSE" in discrepancies[0]

    def test_licence_inside_a_wheel_tree_is_an_extra_file(self, vendor_dir: Path) -> None:
        """Vendoring it inside ``atlas/`` would need a per-file exception — refuse instead."""
        _write(vendor_dir / "atlas" / "LICENSE", _LICENCE_TEXT)
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas/LICENSE: in the vendored tree but not in the manifest"
        ]

    def test_manifest_without_a_licence_entry_and_without_a_sibling_is_clean(self, vendor_dir: Path) -> None:
        """The patched copy's licence is inside its tree, so ``vdf.LICENSE`` must not be demanded."""
        assert not (vendor_dir / "vdf.LICENSE").exists()
        assert check.licence_entry(check.parse_manifest(_generated_manifest_text(_PATCHED_TREE), "x"), "x") is None
        assert check.collect_discrepancies(vendor_dir) == []

    def test_a_sibling_licence_the_manifest_cannot_pin_is_reported(self, vendor_dir: Path) -> None:
        """A licence beside a manifest carrying no entry for it is checked by nothing."""
        _write(vendor_dir / "vdf.LICENSE", "not the licence at all\n")
        assert check.collect_discrepancies(vendor_dir) == [
            "vdf.LICENSE: pinned by nothing — vdf.SHA256SUMS carries no dist-info licence entry to hold it against"
        ]

    def test_regenerating_a_wheel_manifest_cannot_drop_the_licence_check_silently(self, vendor_dir: Path) -> None:
        """The one step that unpins ``atlas.LICENSE`` — and the reason the case above is reported.

        Regenerating a wheel's manifest from the vendored tree is a documented
        procedure for a patched copy, so it is a step someone reaches for. It
        drops the dist-info lines, and with them the only thing holding the
        sibling licence: without this, junking ``atlas.LICENSE`` right after is
        green.
        """
        _write(vendor_dir / _ATLAS_MANIFEST, _generated_manifest_text(_WHEEL_TREE))
        _write(vendor_dir / _ATLAS_LICENCE, "not the licence at all\n")
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas.LICENSE: pinned by nothing — atlas.SHA256SUMS carries no dist-info licence entry to hold it against"
        ]

    def test_manifest_with_two_licence_entries_is_reported(self, vendor_dir: Path) -> None:
        extra = f"{_digest('other')}  emu_atlas-0.6.0.dist-info/licenses/LICENSE\n"
        _write(vendor_dir / _ATLAS_MANIFEST, _wheel_manifest_text(_WHEEL_TREE) + extra)
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas.SHA256SUMS: expected at most one dist-info licence entry, found 2"
        ]


class TestManifest:
    """A manifest that cannot be trusted must fail loudly, never check less."""

    def test_manifest_without_tree_entries_fails(self, vendor_dir: Path) -> None:
        _write(vendor_dir / _ATLAS_MANIFEST, f"{_digest('wheel')}  some-other-project.tar.gz\n")
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas.SHA256SUMS: no 'atlas/' entries — it pins no vendored tree"
        ]

    def test_unparseable_line_is_refused(self) -> None:
        text = f"{_digest('a')}  atlas/__init__.py\nnot a checksum line\n"
        with pytest.raises(check.ManifestError, match="unparseable line 2"):
            check.parse_manifest(text, _ATLAS_MANIFEST)

    def test_duplicate_entry_is_refused(self) -> None:
        line = f"{_digest('a')}  atlas/__init__.py\n"
        with pytest.raises(check.ManifestError, match=re.escape("duplicate entry for 'atlas/__init__.py' on line 2")):
            check.parse_manifest(line * 2, _ATLAS_MANIFEST)

    def test_an_unreadable_manifest_does_not_hide_another_tree(self, vendor_dir: Path) -> None:
        """One bad line refuses its own package and nothing else — the sweep visits every tree.

        Raised out of the loop instead, the first unreadable manifest ends the
        run: the drift below is never reported and ``main`` never reaches the
        remediation guidance it prints alongside the findings.
        """
        _write(vendor_dir / _ATLAS_MANIFEST, _wheel_manifest_text(_WHEEL_TREE) + "not a checksum line\n")
        (vendor_dir / "vdf" / "vdict.py").unlink()
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas.SHA256SUMS: unparseable line 8: 'not a checksum line'",
            "vdf/vdict.py: in the manifest but missing from the vendored tree",
        ]

    def test_a_manifest_that_is_not_utf8_does_not_hide_another_tree(self, vendor_dir: Path) -> None:
        """Undecodable bytes are one package's finding, exactly like an unparseable line.

        The read sits inside the same guard as the parse for this reason: it is
        the parse's other half, and letting it out raises ``UnicodeDecodeError``
        from the middle of the sweep — no findings, no remediation, and every
        tree after this one unchecked.
        """
        (vendor_dir / _ATLAS_MANIFEST).write_bytes(b"\xff\xfe not a manifest\n")
        (vendor_dir / "vdf" / "vdict.py").unlink()
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert discrepancies[0].startswith("atlas.SHA256SUMS: cannot be read — ")
        assert discrepancies[1:] == ["vdf/vdict.py: in the manifest but missing from the vendored tree"]

    def test_a_manifest_that_cannot_be_opened_does_not_hide_another_tree(self, vendor_dir: Path) -> None:
        """A manifest the process cannot open pins nothing, and says so against its own package.

        A dangling symlink is the cheap way to reach the ``OSError`` half
        without depending on file modes, which say nothing when the suite runs
        as root. Discovery is by name, so the manifest is still found and its
        package is still visited — only the read fails.
        """
        manifest = vendor_dir / _ATLAS_MANIFEST
        manifest.unlink()
        manifest.symlink_to(vendor_dir / "gone.SHA256SUMS")
        (vendor_dir / "vdf" / "vdict.py").unlink()
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert discrepancies[0].startswith("atlas.SHA256SUMS: cannot be read — ")
        assert discrepancies[1:] == ["vdf/vdict.py: in the manifest but missing from the vendored tree"]

    def test_blank_lines_are_skipped(self) -> None:
        text = f"\n{_digest('a')}  atlas/__init__.py\n\n"
        assert check.parse_manifest(text, _ATLAS_MANIFEST) == {"atlas/__init__.py": _digest("a")}

    def test_binary_mode_marker_parses(self) -> None:
        """``sha256sum -b`` writes ``digest *path`` — accept it rather than reject the manifest."""
        text = f"{_digest('a')} *atlas/py.typed\n"
        assert check.parse_manifest(text, _ATLAS_MANIFEST) == {"atlas/py.typed": _digest("a")}


class TestExitCodes:
    """``main`` is what CI reads: an exit code plus guidance a human can act on."""

    def test_clean_repository_copies_exit_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert check.main() == 0
        assert "OK: every vendored tree matches" in capsys.readouterr().out

    def test_drift_exits_one_with_remediation(
        self, vendor_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(vendor_dir / "atlas" / "__init__.py", "tampered\n")
        monkeypatch.setattr(check, "VENDOR_DIR", vendor_dir)
        assert check.main() == 1
        err = capsys.readouterr().err
        assert "atlas/__init__.py: digest " in err
        assert "xargs sha256sum > <pkg>.SHA256SUMS" in err
        assert "py_modules/_vendor/README.md" in err

    def test_an_unreadable_manifest_still_reaches_the_remediation(
        self, vendor_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A refusal is a finding like any other, so the guidance a human acts on still prints."""
        _write(vendor_dir / _ATLAS_MANIFEST, _wheel_manifest_text(_WHEEL_TREE) + "not a checksum line\n")
        monkeypatch.setattr(check, "VENDOR_DIR", vendor_dir)
        assert check.main() == 1
        err = capsys.readouterr().err
        assert "atlas.SHA256SUMS: unparseable line 8:" in err
        assert "xargs sha256sum > <pkg>.SHA256SUMS" in err
