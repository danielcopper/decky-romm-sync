"""Tests for ``scripts/check_vendored_atlas.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). ``collect_discrepancies``
takes the vendor directory explicitly, so most cases lay out a small synthetic
``_vendor/`` under ``tmp_path`` — a three-file ``atlas/`` tree, its licence and a
manifest — and the real comparison logic runs against it. The parser cases need
no tree at all, and two cases deliberately use the real one.

Coverage centres on the four ways a vendored copy stops being the release it
pins — a file tampered with, one deleted, one added, and a licence that no
longer matches — because a checksum gate that misses any of them is decorative.
The deleted and added cases are the reason this gate exists at all: ``sha256sum
-c --ignore-missing`` exits 0 on both. The manifest parser's own refusals
(unparseable line, duplicate entry, a licence entry that is missing or
ambiguous) are pinned too, since each would otherwise drop a check silently.
Two tests run against the REAL vendored tree — one over ``collect_discrepancies``
and one over ``main`` — so the copy in this repository is verified by ``mise run
test`` and not only by ``mise run lint``.
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

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_vendored_atlas.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_vendored_atlas", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()

_LICENCE_TEXT = "MIT License\n\nCopyright (c) 2026 danielcopper\n"

# The synthetic tree: enough shape to exercise the walk (a nested data file, a
# non-Python marker file) without copying the real 52-file package.
_TREE = {
    "atlas/__init__.py": '__version__ = "0.5.0"\n',
    "atlas/data/system_ids.json": '{"snes": 4}\n',
    "atlas/py.typed": "",
}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _manifest_text(tree: dict[str, str], licence: str = _LICENCE_TEXT) -> str:
    """A manifest in upstream's shape: release artifacts, the tree, the dist-info.

    The non-``atlas/`` entries are deliberate — they cover files that are NOT
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


@pytest.fixture
def vendor_dir(tmp_path: Path) -> Path:
    """A synthetic ``_vendor/`` whose atlas copy matches its manifest exactly."""
    root = tmp_path / "_vendor"
    for relative, text in _TREE.items():
        _write(root / relative, text)
    _write(root / check.LICENSE_NAME, _LICENCE_TEXT)
    _write(root / check.MANIFEST_NAME, _manifest_text(_TREE))
    return root


class TestCleanCopy:
    """A copy that is exactly the pinned release must pass — and say nothing else."""

    def test_matching_copy_has_no_discrepancies(self, vendor_dir: Path) -> None:
        assert check.collect_discrepancies(vendor_dir) == []

    def test_pycache_is_ignored(self, vendor_dir: Path) -> None:
        """Importing the tree writes bytecode that was never in the manifest."""
        _write(vendor_dir / "atlas" / "__pycache__" / "__init__.cpython-311.pyc", "not really bytecode")
        assert check.collect_discrepancies(vendor_dir) == []

    def test_the_real_vendored_tree_matches_its_manifest(self) -> None:
        """The gate run against the copy this repository actually ships."""
        assert check.collect_discrepancies(check.VENDOR_DIR) == []


class TestDrift:
    """The ways a copy's file set stops being the release it pins.

    Tampered, deleted, added — and the shapes those take at scale. The fourth
    way, a licence that no longer matches, lives in ``TestLicence``.
    """

    def test_tampered_file_fails(self, vendor_dir: Path) -> None:
        _write(vendor_dir / "atlas" / "__init__.py", '__version__ = "0.5.0"  # local fix\n')
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert len(discrepancies) == 1
        assert discrepancies[0].startswith("atlas/__init__.py: digest ")

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
        """Right content, wrong kind of thing — the wheel ships no symlinks.

        Without the guard in ``vendored_tree_files`` this passes: ``is_file()``
        follows the link, the digest is read through to a target holding the
        manifest's own bytes, and nothing is reported.
        """
        target = tmp_path / "outside.py"
        target.write_text(_TREE["atlas/__init__.py"], encoding="utf-8")
        victim = vendor_dir / "atlas" / "__init__.py"
        victim.unlink()
        victim.symlink_to(target)
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas/__init__.py: in the manifest but missing from the vendored tree"
        ]

    def test_a_missing_tree_reports_every_file(self, vendor_dir: Path) -> None:
        """The whole package gone must report, not raise on the absent directory."""
        for relative in _TREE:
            (vendor_dir / relative).unlink()
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert len(discrepancies) == len(_TREE)
        assert all("missing from the vendored tree" in line for line in discrepancies)

    def test_every_drifted_file_is_reported(self, vendor_dir: Path) -> None:
        """One line per discrepancy — a first failure must not mask the rest."""
        _write(vendor_dir / "atlas" / "__init__.py", "tampered\n")
        (vendor_dir / "atlas" / "py.typed").unlink()
        _write(vendor_dir / "atlas" / "extra.py", "")
        assert len(check.collect_discrepancies(vendor_dir)) == 3


class TestLicence:
    """The licence is vendored as a sibling of the tree, so it needs its own check."""

    def test_missing_licence_fails(self, vendor_dir: Path) -> None:
        (vendor_dir / check.LICENSE_NAME).unlink()
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas.LICENSE: missing — upstream's licence, copied from emu_atlas-0.5.0.dist-info/licenses/LICENSE"
        ]

    def test_tampered_licence_fails(self, vendor_dir: Path) -> None:
        _write(vendor_dir / check.LICENSE_NAME, _LICENCE_TEXT.replace("MIT", "GPL"))
        discrepancies = check.collect_discrepancies(vendor_dir)
        assert len(discrepancies) == 1
        assert discrepancies[0].startswith("atlas.LICENSE: digest ")

    def test_licence_inside_the_tree_is_an_extra_file(self, vendor_dir: Path) -> None:
        """Vendoring it inside ``atlas/`` would need a per-file exception — refuse instead."""
        _write(vendor_dir / "atlas" / "LICENSE", _LICENCE_TEXT)
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas/LICENSE: in the vendored tree but not in the manifest"
        ]

    def test_manifest_without_a_licence_entry_is_refused(self, vendor_dir: Path) -> None:
        lines = [line for line in _manifest_text(_TREE).splitlines() if "licenses/LICENSE" not in line]
        _write(vendor_dir / check.MANIFEST_NAME, "\n".join(lines) + "\n")
        with pytest.raises(SystemExit, match="exactly one dist-info licence entry, found 0"):
            check.collect_discrepancies(vendor_dir)

    def test_manifest_with_two_licence_entries_is_refused(self, vendor_dir: Path) -> None:
        extra = f"{_digest('other')}  emu_atlas-0.6.0.dist-info/licenses/LICENSE\n"
        _write(vendor_dir / check.MANIFEST_NAME, _manifest_text(_TREE) + extra)
        with pytest.raises(SystemExit, match="exactly one dist-info licence entry, found 2"):
            check.collect_discrepancies(vendor_dir)


class TestManifest:
    """A manifest that cannot be trusted must fail loudly, never check less."""

    def test_missing_manifest_fails(self, vendor_dir: Path) -> None:
        (vendor_dir / check.MANIFEST_NAME).unlink()
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas.SHA256SUMS: missing — the vendored copy pins no release at all"
        ]

    def test_manifest_without_tree_entries_fails(self, vendor_dir: Path) -> None:
        _write(vendor_dir / check.MANIFEST_NAME, f"{_digest('wheel')}  some-other-project.tar.gz\n")
        assert check.collect_discrepancies(vendor_dir) == [
            "atlas.SHA256SUMS: no 'atlas/' entries — this is not an emu-atlas wheel manifest"
        ]

    def test_unparseable_line_is_refused(self) -> None:
        text = f"{_digest('a')}  atlas/__init__.py\nnot a checksum line\n"
        with pytest.raises(SystemExit, match="unparseable line 2"):
            check.parse_manifest(text)

    def test_duplicate_entry_is_refused(self) -> None:
        line = f"{_digest('a')}  atlas/__init__.py\n"
        with pytest.raises(SystemExit, match=re.escape("duplicate entry for 'atlas/__init__.py' on line 2")):
            check.parse_manifest(line * 2)

    def test_blank_lines_are_skipped(self) -> None:
        assert check.parse_manifest(f"\n{_digest('a')}  atlas/__init__.py\n\n") == {"atlas/__init__.py": _digest("a")}

    def test_binary_mode_marker_parses(self) -> None:
        """``sha256sum -b`` writes ``digest *path`` — accept it rather than reject the manifest."""
        assert check.parse_manifest(f"{_digest('a')} *atlas/py.typed\n") == {"atlas/py.typed": _digest("a")}


class TestExitCodes:
    """``main`` is what CI reads: an exit code plus guidance a human can act on."""

    def test_clean_repository_copy_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert check.main() == 0
        assert "OK: the vendored emu-atlas copy matches" in capsys.readouterr().out

    def test_drift_exits_one_with_remediation(
        self, vendor_dir: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(vendor_dir / "atlas" / "__init__.py", "tampered\n")
        monkeypatch.setattr(check, "VENDOR_DIR", vendor_dir)
        assert check.main() == 1
        err = capsys.readouterr().err
        assert "atlas/__init__.py: digest " in err
        assert "gh release download <tag> -R danielcopper/emu-atlas" in err
        assert "py_modules/_vendor/README.md" in err
