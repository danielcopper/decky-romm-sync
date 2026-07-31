"""Tests for ``scripts/check_markdown_links.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Link-resolution tests
lay out small ``.md`` trees under ``tmp_path`` and drive
``collect_broken_links`` / ``main`` directly — both take an explicit
``repo_root``, so the resolver walks the temporary tree, not the real repo.
``tracked_markdown_files`` (and its ``CHANGELOG.md`` / untracked-tree skips) is
exercised against a throwaway ``git init`` repo.

Coverage centres on the two checks the gate makes — file existence (resolved
relative to the linking file) and ``#anchor`` membership (python-markdown ``toc``
slug + ``attr_list`` id, unioned) — plus the deliberate skips (external links,
images, fenced/inline code) and the slug edge cases (separator collapse,
duplicate-heading ``_1`` suffix).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_markdown_links.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_markdown_links", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


def _write(root: Path, name: str, content: str) -> Path:
    """Write ``root/name`` with dedented, left-stripped *content*; return its path."""
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return path


# ── File-target resolution ───────────────────────────────────────────────


class TestFileTargetResolution:
    def test_resolvable_file_and_anchor_is_clean(self, tmp_path: Path):
        _write(tmp_path, "b.md", "## Heading Here\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#heading-here).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_missing_target_file_is_broken(self, tmp_path: Path):
        source = _write(tmp_path, "a.md", "See [x](missing.md).\n")
        broken = check.collect_broken_links([source], tmp_path)
        assert len(broken) == 1
        assert broken[0].reason == "target file not found"
        assert broken[0].link == "missing.md"

    def test_relative_parent_dir_resolves(self, tmp_path: Path):
        _write(tmp_path, "b.md", "## Top\n")
        source = _write(tmp_path, "sub/a.md", "See [x](../b.md#top).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_file_only_link_without_anchor(self, tmp_path: Path):
        _write(tmp_path, "b.md", "no headings here\n")
        source = _write(tmp_path, "a.md", "See [x](b.md).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_link_title_is_stripped(self, tmp_path: Path):
        _write(tmp_path, "b.md", "## Heading Here\n")
        source = _write(tmp_path, "a.md", 'See [x](b.md#heading-here "A Title").\n')
        assert check.collect_broken_links([source], tmp_path) == []


# ── Anchor membership ────────────────────────────────────────────────────


class TestAnchorMembership:
    def test_missing_anchor_is_broken(self, tmp_path: Path):
        _write(tmp_path, "b.md", "## Heading Here\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#no-such-anchor).\n")
        broken = check.collect_broken_links([source], tmp_path)
        assert len(broken) == 1
        assert "anchor '#no-such-anchor' not found" in broken[0].reason

    def test_pure_anchor_self_link_resolves(self, tmp_path: Path):
        source = _write(tmp_path, "a.md", "## My Section\n\nJump [up](#my-section).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_pure_anchor_self_link_broken(self, tmp_path: Path):
        source = _write(tmp_path, "a.md", "## My Section\n\nJump [up](#wrong).\n")
        broken = check.collect_broken_links([source], tmp_path)
        assert len(broken) == 1
        assert "anchor '#wrong' not found" in broken[0].reason

    def test_anchor_from_inline_markdown_heading(self, tmp_path: Path):
        # ``## The `Foo` bar`` renders to text "The Foo bar" -> slug "the-foo-bar".
        _write(tmp_path, "b.md", "## The `Foo` **bar**\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#the-foo-bar).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_non_md_target_anchor_not_checked(self, tmp_path: Path):
        # An anchor into a non-.md target (e.g. an image) is not slug-checked.
        _write(tmp_path, "pic.svg", "<svg/>\n")
        source = _write(tmp_path, "a.md", "See [x](pic.svg#whatever).\n")
        assert check.collect_broken_links([source], tmp_path) == []


class TestAttrListIds:
    def test_custom_id_resolves(self, tmp_path: Path):
        _write(tmp_path, "b.md", "## A Heading {#custom-id}\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#custom-id).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_explicit_id_replaces_auto_slug(self, tmp_path: Path):
        # attr_list REPLACES the auto-slug: the engine emits ``custom-id`` only,
        # so ``#a-heading`` is a dead anchor and must be reported.
        _write(tmp_path, "b.md", "## A Heading {#custom-id}\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#a-heading).\n")
        broken = check.collect_broken_links([source], tmp_path)
        assert len(broken) == 1
        assert "anchor '#a-heading' not found" in broken[0].reason

    def test_class_only_attr_block_keeps_auto_slug(self, tmp_path: Path):
        # A ``{.cls}`` block carries no id, so the heading still gets its slug.
        _write(tmp_path, "b.md", "## A Heading {.cls}\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#a-heading).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_explicit_id_seeds_the_dedup_pool(self, tmp_path: Path):
        # The engine pre-registers explicit ids before assigning any auto-slug,
        # so a heading whose slug collides with a ``{#dup}`` becomes ``dup_1``.
        _write(tmp_path, "b.md", "## A {#dup}\n\n## Dup\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#dup) and [y](b.md#dup_1).\n")
        assert check.collect_broken_links([source], tmp_path) == []


class TestUnderscoreEmphasis:
    def test_word_boundary_emphasis_is_stripped(self, tmp_path: Path):
        _write(tmp_path, "b.md", "## The _foo_ bar\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#the-foo-bar).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_intra_word_underscore_is_kept(self, tmp_path: Path):
        # ``file_path`` is literal text, not emphasis — the underscore survives
        # slugify as a word char, so the anchor keeps it.
        _write(tmp_path, "b.md", "## keep file_path intact\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#keep-file_path-intact).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_strong_underscore_is_stripped(self, tmp_path: Path):
        _write(tmp_path, "b.md", "## __strong__ heading\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#strong-heading).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_underscores_inside_code_span_are_literal(self, tmp_path: Path):
        # Emphasis rules do not apply inside a code span: ``__init__.py`` keeps
        # its underscores (real CLAUDE.md heading shape).
        _write(tmp_path, "b.md", "## Sub-package `__init__.py` — when populated\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#sub-package-__init__py-when-populated).\n")
        assert check.collect_broken_links([source], tmp_path) == []


class TestHeadingRecognitionEdges:
    def test_no_space_after_hash_is_still_a_heading(self, tmp_path: Path):
        # python-markdown does not require whitespace after the hashes.
        _write(tmp_path, "b.md", "#1210 no space here\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#1210-no-space-here).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_indented_hash_line_is_not_a_heading(self, tmp_path: Path):
        # An indented continuation line is paragraph text, not a heading, so it
        # claims no anchor (python-markdown allows no leading indent).
        _write(tmp_path, "b.md", "- bullet wrapping onto\n  #1032 (after the sentinels)\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#1032-after-the-sentinels).\n")
        broken = check.collect_broken_links([source], tmp_path)
        assert len(broken) == 1
        assert "anchor '#1032-after-the-sentinels' not found" in broken[0].reason


class TestSeparatorCollapse:
    def test_slash_collapses_to_single_hyphen(self, tmp_path: Path):
        # MkDocs collapses ``A / b`` to ``a-b`` (a single hyphen), not ``a--b``.
        _write(tmp_path, "b.md", "## A / b\n")
        good = _write(tmp_path, "a.md", "See [x](b.md#a-b).\n")
        assert check.collect_broken_links([good], tmp_path) == []

    def test_double_hyphen_link_is_broken(self, tmp_path: Path):
        _write(tmp_path, "b.md", "## A / b\n")
        bad = _write(tmp_path, "a.md", "See [x](b.md#a--b).\n")
        broken = check.collect_broken_links([bad], tmp_path)
        assert len(broken) == 1
        assert "anchor '#a--b' not found" in broken[0].reason


class TestDuplicateHeadings:
    def test_underscore_suffix_resolves(self, tmp_path: Path):
        # python-markdown suffixes a repeated heading with ``_1`` (underscore).
        _write(tmp_path, "b.md", "# Dup\n\n# Dup\n")
        source = _write(tmp_path, "a.md", "See [x](b.md#dup) and [y](b.md#dup_1).\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_hyphen_suffix_link_is_broken(self, tmp_path: Path):
        # ``#dup-1`` (GitHub style) does NOT resolve under MkDocs.
        _write(tmp_path, "b.md", "# Dup\n\n# Dup\n")
        source = _write(tmp_path, "a.md", "See [y](b.md#dup-1).\n")
        broken = check.collect_broken_links([source], tmp_path)
        assert len(broken) == 1
        assert "anchor '#dup-1' not found" in broken[0].reason


# ── Deliberate skips ─────────────────────────────────────────────────────


class TestExternalLinksSkipped:
    def test_external_schemes_never_checked(self, tmp_path: Path):
        source = _write(
            tmp_path,
            "a.md",
            """
            [a](http://example.com/x)
            [b](https://example.com/y#frag)
            [c](mailto:foo@bar.com)
            [d](tel:+1234567890)
            [e](//cdn.example.com/z)
            """,
        )
        assert check.collect_broken_links([source], tmp_path) == []


class TestImagesSkipped:
    def test_image_not_checked_but_sibling_link_is(self, tmp_path: Path):
        # The image is skipped; the plain link to the same missing file is caught,
        # proving the ``!`` prefix (not the whole line) is what excludes it.
        source = _write(tmp_path, "a.md", "![alt](missing.png) then [link](missing.png)\n")
        broken = check.collect_broken_links([source], tmp_path)
        assert len(broken) == 1
        assert broken[0].link == "missing.png"


class TestCodeSkipped:
    def test_fenced_code_links_skipped(self, tmp_path: Path):
        source = _write(
            tmp_path,
            "a.md",
            """
            ```
            [x](missing.md)
            ```
            """,
        )
        assert check.collect_broken_links([source], tmp_path) == []

    def test_inline_code_links_skipped(self, tmp_path: Path):
        source = _write(tmp_path, "a.md", "Use `[x](missing.md)` literally.\n")
        assert check.collect_broken_links([source], tmp_path) == []

    def test_heading_inside_fence_is_not_an_anchor(self, tmp_path: Path):
        # A ``#``-prefixed line inside a code fence is not a heading, so linking
        # to its would-be slug is broken.
        _write(
            tmp_path,
            "b.md",
            """
            ```bash
            # Not A Heading
            echo hi
            ```
            """,
        )
        source = _write(tmp_path, "a.md", "See [x](b.md#not-a-heading).\n")
        broken = check.collect_broken_links([source], tmp_path)
        assert len(broken) == 1
        assert "anchor '#not-a-heading' not found" in broken[0].reason


# ── slugify unit behaviour ───────────────────────────────────────────────


class TestSlugify:
    def test_collapses_separator_runs(self):
        assert check.slugify("A / b") == "a-b"

    def test_drops_punctuation_and_lowercases(self):
        assert check.slugify("Recovery after a server switch / re-import") == "recovery-after-a-server-switch-re-import"

    def test_keeps_hyphens_and_underscores(self):
        assert check.slugify("ES-DE core_choice") == "es-de-core_choice"


# ── tracked_markdown_files (git ls-files source) ─────────────────────────


def _git_repo(tmp_path: Path, tracked: dict[str, str], untracked: dict[str, str] | None = None) -> Path:
    """Init a git repo in *tmp_path*, add *tracked* files, write *untracked* ones."""
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name, content in tracked.items():
        _write(tmp_path, name, content)
        subprocess.run(["git", "add", name], cwd=tmp_path, check=True)
    for name, content in (untracked or {}).items():
        _write(tmp_path, name, content)
    return tmp_path


class TestTrackedMarkdownFiles:
    def test_lists_tracked_md_skips_changelog_and_untracked(self, tmp_path: Path):
        _git_repo(
            tmp_path,
            tracked={"a.md": "# a\n", "docs/b.md": "# b\n", "CHANGELOG.md": "# cl\n"},
            untracked={".venv/x.md": "# venv\n"},
        )
        names = {p.name for p in check.tracked_markdown_files(tmp_path)}
        assert names == {"a.md", "b.md"}

    def test_exempt_path_excluded(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        _git_repo(tmp_path, tracked={"a.md": "# a\n", "b.md": "# b\n"})
        monkeypatch.setattr(check, "EXEMPT", frozenset({"b.md"}))
        names = {p.name for p in check.tracked_markdown_files(tmp_path)}
        assert names == {"a.md"}


# ── main() entry point ───────────────────────────────────────────────────


class TestMainEntryPoint:
    def test_help_flag_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        assert check.main(["--help"]) == 0
        assert "link-integrity" in capsys.readouterr().out

    def test_short_help_flag_returns_zero(self):
        assert check.main(["-h"]) == 0

    def test_real_repo_is_clean(self, capsys: pytest.CaptureFixture[str]):
        # The real docs tree must resolve (the two seeded breakages are fixed).
        assert check.main([]) == 0
        assert "OK:" in capsys.readouterr().out

    def test_broken_repo_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        _git_repo(tmp_path, tracked={"a.md": "See [x](missing.md).\n"})
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        assert check.main([]) == 1
        out = capsys.readouterr().out
        assert "ERROR:" in out
        assert "a.md -> missing.md" in out
