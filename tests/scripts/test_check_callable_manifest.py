"""Tests for ``scripts/check_callable_manifest.py``.

Loaded via ``importlib`` because ``scripts/`` is not on ``sys.path`` (and is
excluded from ruff/basedpyright). Parser edge cases feed synthetic ``.ts`` /
``main.py`` files under ``tmp_path``; the happy-path test asserts the real repo
tree is clean (the regression that protects the live frontend↔backend wire).
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_callable_manifest.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_callable_manifest", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


def _write_ts(tmp_path: Path, name: str, body: str) -> Path:
    """Write a ``.ts`` file under a fake ``src/`` tree and return the src dir."""
    src = tmp_path / "src"
    src.mkdir(exist_ok=True)
    (src / name).write_text(body, encoding="utf-8")
    return src


def _write_main(tmp_path: Path, body: str) -> Path:
    """Write a synthetic ``main.py`` with a ``Plugin`` class body and return its path."""
    path = tmp_path / "main.py"
    path.write_text(body, encoding="utf-8")
    return path


class TestParseFrontendCallables:
    def test_empty_args_is_arity_zero(self, tmp_path: Path):
        src = _write_ts(tmp_path, "a.ts", 'const x = callable<[], Foo>("get_settings");')
        assert check.parse_frontend_callables(src) == {"get_settings": 0}

    def test_single_arg_is_arity_one(self, tmp_path: Path):
        src = _write_ts(tmp_path, "a.ts", 'callable<[number], Foo>("start_download");')
        assert check.parse_frontend_callables(src) == {"start_download": 1}

    def test_three_args_with_union(self, tmp_path: Path):
        src = _write_ts(tmp_path, "a.ts", 'callable<[number, string, string | null], Foo>("f");')
        assert check.parse_frontend_callables(src) == {"f": 3}

    def test_nested_generic_comma_not_counted(self, tmp_path: Path):
        # Record<string, number> is ONE element — the inner comma must not inflate.
        # Mirrors report_unit_results: Record<…> + run_id + unit_id → arity 3.
        src = _write_ts(
            tmp_path,
            "a.ts",
            'callable<[Record<string, number>, string, number | string], Foo>("report_unit_results");',
        )
        assert check.parse_frontend_callables(src) == {"report_unit_results": 3}

    def test_union_with_string_literals_is_arity_two(self, tmp_path: Path):
        src = _write_ts(tmp_path, "a.ts", 'callable<[boolean, "my" | "smart" | null], Foo>("g");')
        assert check.parse_frontend_callables(src) == {"g": 2}

    def test_object_literal_return_does_not_split_args(self, tmp_path: Path):
        src = _write_ts(tmp_path, "a.ts", 'callable<[number], { a: number; b: string }>("h");')
        assert check.parse_frontend_callables(src) == {"h": 1}

    def test_multiline_declaration(self, tmp_path: Path):
        body = textwrap.dedent(
            """\
            export const setAllCollectionsSync = callable<
              [boolean, "my" | "smart" | "franchise" | null],
              { success: boolean; message?: string }
            >("set_all_collections_sync");
            """
        )
        src = _write_ts(tmp_path, "a.ts", body)
        assert check.parse_frontend_callables(src) == {"set_all_collections_sync": 2}

    def test_string_literal_containing_bracket_and_comma(self, tmp_path: Path):
        # A string literal in the args tuple must not be parsed as structure.
        src = _write_ts(tmp_path, "a.ts", 'callable<["a, b]", number], Foo>("weird");')
        assert check.parse_frontend_callables(src) == {"weird": 2}

    def test_scans_tsx_and_nested_dirs(self, tmp_path: Path):
        src = tmp_path / "src"
        (src / "utils").mkdir(parents=True)
        (src / "api.ts").write_text('callable<[], A>("a");', encoding="utf-8")
        (src / "utils" / "store.tsx").write_text('callable<[number], B>("b");', encoding="utf-8")
        assert check.parse_frontend_callables(src) == {"a": 0, "b": 1}

    def test_duplicate_name_marked_sentinel(self, tmp_path: Path):
        src = _write_ts(tmp_path, "a.ts", 'callable<[], A>("dup");\ncallable<[number], B>("dup");')
        assert check.parse_frontend_callables(src) == {"dup": -1}

    def test_missing_src_returns_empty(self, tmp_path: Path):
        assert check.parse_frontend_callables(tmp_path / "nope") == {}


class TestParserHardening:
    """Regression cases for the adversarial blind spots: TS comments, arrow-typed
    args, trailing commas, and string-literal preservation of comment-like text.
    Each test below fails against the pre-hardening parser."""

    def test_comment_with_apostrophe_inside_generic(self, tmp_path: Path):
        # A line comment carrying an apostrophe inside the multiline <...> block
        # must not corrupt depth tracking — the callable is still captured.
        body = textwrap.dedent(
            """\
            export const x = callable<
              [number],
              // it's a tricky comment
              R
            >("has_apostrophe");
            """
        )
        src = _write_ts(tmp_path, "a.ts", body)
        assert check.parse_frontend_callables(src) == {"has_apostrophe": 1}

    def test_comment_with_brackets_and_angles_inside_generic(self, tmp_path: Path):
        # A comment with an unbalanced bracket and a stray > must be ignored.
        body = textwrap.dedent(
            """\
            export const y = callable<
              [number, string],
              // compares x > y and references app_ids[]
              R
            >("noisy_comment");
            """
        )
        src = _write_ts(tmp_path, "a.ts", body)
        assert check.parse_frontend_callables(src) == {"noisy_comment": 2}

    def test_block_comment_inside_generic(self, tmp_path: Path):
        body = textwrap.dedent(
            """\
            export const z = callable<
              [number, /* inline 's apostrophe, < > [ */ string],
              R
            >("block_comment");
            """
        )
        src = _write_ts(tmp_path, "a.ts", body)
        assert check.parse_frontend_callables(src) == {"block_comment": 2}

    def test_real_shape_multiline_with_apostrophe_comment(self, tmp_path: Path):
        # Mirrors the remove_platform_shortcuts declaration: a multiline generic
        # whose Return carries inline comments with apostrophes.
        body = textwrap.dedent(
            """\
            export const removePlatformShortcuts = callable<
              [string],
              {
                success: boolean;
                // The success path returns success/app_ids/rom_ids; the
                // @migration_blocked gate short-circuits, so it's path-dependent.
                app_ids?: number[];
                rom_ids?: (string | number)[];
                message?: string;
              }
            >("remove_platform_shortcuts");
            """
        )
        src = _write_ts(tmp_path, "a.ts", body)
        assert check.parse_frontend_callables(src) == {"remove_platform_shortcuts": 1}

    def test_commented_out_declaration_not_counted(self, tmp_path: Path):
        body = textwrap.dedent(
            """\
            // const dead = callable<[number], R>("dead");
            const live = callable<[], R>("alive");
            """
        )
        src = _write_ts(tmp_path, "a.ts", body)
        result = check.parse_frontend_callables(src)
        assert "dead" not in result
        assert result == {"alive": 0}

    def test_block_commented_out_declaration_not_counted(self, tmp_path: Path):
        body = textwrap.dedent(
            """\
            /* const dead = callable<[number], R>("dead2"); */
            const live = callable<[number], R>("alive2");
            """
        )
        src = _write_ts(tmp_path, "a.ts", body)
        result = check.parse_frontend_callables(src)
        assert "dead2" not in result
        assert result == {"alive2": 1}

    def test_arrow_function_typed_arg_is_arity_one(self, tmp_path: Path):
        # The > in => must not unbalance the generic's depth.
        src = _write_ts(tmp_path, "a.ts", 'callable<[(a: number, b: number) => void], R>("arrow_arg");')
        assert check.parse_frontend_callables(src) == {"arrow_arg": 1}

    def test_trailing_comma_does_not_inflate_arity(self, tmp_path: Path):
        src = _write_ts(tmp_path, "a.ts", 'callable<[number, string,], R>("trailing_comma");')
        assert check.parse_frontend_callables(src) == {"trailing_comma": 2}

    def test_comment_like_text_in_string_literal_preserved(self, tmp_path: Path):
        # A // or /* inside a string literal is part of the string, NOT a comment.
        src = _write_ts(tmp_path, "a.ts", 'callable<["http://x.com/*nope", number], R>("url_in_string");')
        assert check.parse_frontend_callables(src) == {"url_in_string": 2}


class TestParseBackendCallables:
    def test_public_methods_with_arity(self, tmp_path: Path):
        body = textwrap.dedent(
            """\
            class Plugin:
                async def get_settings(self):
                    ...
                async def start_download(self, rom_id):
                    ...
                async def switch_slot(self, rom_id, new_slot):
                    ...
            """
        )
        main_py = _write_main(tmp_path, body)
        assert check.parse_backend_callables(main_py) == {
            "get_settings": 0,
            "start_download": 1,
            "switch_slot": 2,
        }

    def test_underscore_internal_excluded(self, tmp_path: Path):
        body = textwrap.dedent(
            """\
            class Plugin:
                async def _main(self):
                    ...
                async def _unload(self):
                    ...
                async def test_connection(self):
                    ...
            """
        )
        main_py = _write_main(tmp_path, body)
        assert check.parse_backend_callables(main_py) == {"test_connection": 0}

    def test_default_param_counts_as_positional_slot(self, tmp_path: Path):
        body = textwrap.dedent(
            """\
            class Plugin:
                async def connect_with_credentials(self, url, user, pw, allow_insecure_ssl=None):
                    ...
            """
        )
        main_py = _write_main(tmp_path, body)
        assert check.parse_backend_callables(main_py) == {"connect_with_credentials": 4}

    def test_vararg_method_has_none_arity_name_recorded(self, tmp_path: Path):
        body = textwrap.dedent(
            """\
            class Plugin:
                async def flexible(self, *args):
                    ...
            """
        )
        main_py = _write_main(tmp_path, body)
        assert check.parse_backend_callables(main_py) == {"flexible": None}

    def test_sync_methods_ignored(self, tmp_path: Path):
        body = textwrap.dedent(
            """\
            class Plugin:
                def helper(self):
                    ...
                async def real_callable(self):
                    ...
            """
        )
        main_py = _write_main(tmp_path, body)
        assert check.parse_backend_callables(main_py) == {"real_callable": 0}

    def test_no_plugin_class_returns_empty(self, tmp_path: Path):
        main_py = _write_main(tmp_path, "class Other:\n    async def foo(self):\n        ...\n")
        assert check.parse_backend_callables(main_py) == {}

    def test_keyword_only_args_excluded_from_positional_arity(self, tmp_path: Path):
        # ``*, c`` is keyword-only — a positional frontend tuple can't fill it,
        # so arity counts only the positional params (a, b) -> 2.
        body = textwrap.dedent(
            """\
            class Plugin:
                async def m(self, a, b, *, c):
                    ...
            """
        )
        main_py = _write_main(tmp_path, body)
        assert check.parse_backend_callables(main_py) == {"m": 2}

    def test_kwargs_only_yields_positional_arity(self, tmp_path: Path):
        # **kwargs is not positional — the two positional params still count as 2.
        body = textwrap.dedent(
            """\
            class Plugin:
                async def m(self, a, b, **kwargs):
                    ...
            """
        )
        main_py = _write_main(tmp_path, body)
        assert check.parse_backend_callables(main_py) == {"m": 2}


class TestFindDiscrepancies:
    def test_matching_surfaces_no_findings(self):
        fe = {"a": 0, "b": 1}
        be: dict[str, int | None] = {"a": 0, "b": 1}
        assert check.find_discrepancies(fe, be, frozenset()) == []

    def test_frontend_only_name_flagged(self):
        findings = check.find_discrepancies({"a": 0, "ghost": 1}, {"a": 0}, frozenset())
        assert len(findings) == 1
        assert "ghost" in findings[0]
        assert "no public" in findings[0]

    def test_backend_only_name_flagged(self):
        findings = check.find_discrepancies({"a": 0}, {"a": 0, "orphan": 1}, frozenset())
        assert len(findings) == 1
        assert "orphan" in findings[0]
        assert "no frontend" in findings[0]

    def test_arity_mismatch_shows_both_numbers(self):
        findings = check.find_discrepancies({"f": 2}, {"f": 3}, frozenset())
        assert len(findings) == 1
        assert "arity mismatch" in findings[0]
        assert "2 arg" in findings[0]
        assert "takes 3" in findings[0]

    def test_vararg_backend_skips_arity_check(self):
        # Name matches; backend is *args (None) -> no arity finding.
        assert check.find_discrepancies({"f": 5}, {"f": None}, frozenset()) == []

    def test_duplicate_frontend_name_flagged(self):
        findings = check.find_discrepancies({"dup": -1}, {"dup": 1}, frozenset())
        assert any("more than once" in line for line in findings)

    def test_exempt_name_not_flagged_for_presence(self):
        # Frontend-only name, but EXEMPT -> no presence finding.
        assert check.find_discrepancies({"a": 0, "fe_only": 1}, {"a": 0}, frozenset({"fe_only"})) == []


class TestMainEntryPoint:
    def test_help_flag_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        rc = check.main(["--help"])
        assert rc == 0
        assert "callable-manifest parity gate" in capsys.readouterr().out

    def test_short_help_flag_returns_zero(self):
        assert check.main(["-h"]) == 0

    def test_real_repo_run_is_clean(self, capsys: pytest.CaptureFixture[str]):
        # Locks the actual src/**/*.ts callable declarations in sync with the
        # Plugin async methods in main.py. If this fails, a callable was
        # added/renamed/removed on one side only, or an arity drifted.
        rc = check.main([])
        assert rc == 0
        assert "OK:" in capsys.readouterr().out

    def test_drift_reports_and_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        src = _write_ts(tmp_path, "a.ts", 'callable<[], A>("present");\ncallable<[number], B>("frontend_only");')
        main_py = _write_main(
            tmp_path,
            "class Plugin:\n    async def present(self):\n        ...\n",
        )
        monkeypatch.setattr(check, "SRC_DIR", src)
        monkeypatch.setattr(check, "MAIN_PY", main_py)
        monkeypatch.setattr(check, "EXEMPT", frozenset())
        rc = check.main([])
        assert rc == 1
        out = capsys.readouterr().out
        assert "frontend_only" in out
        assert "ERROR:" in out

    def test_in_sync_fake_repo_returns_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        src = _write_ts(tmp_path, "a.ts", 'callable<[number], A>("match");')
        main_py = _write_main(tmp_path, "class Plugin:\n    async def match(self, rom_id):\n        ...\n")
        monkeypatch.setattr(check, "SRC_DIR", src)
        monkeypatch.setattr(check, "MAIN_PY", main_py)
        monkeypatch.setattr(check, "EXEMPT", frozenset())
        assert check.main([]) == 0
