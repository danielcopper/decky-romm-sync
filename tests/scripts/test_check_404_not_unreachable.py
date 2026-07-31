"""Tests for ``scripts/check_404_not_unreachable.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Fixtures use
``tmp_path`` to lay out a small ``py_modules/services/`` tree the check
walks, passing that directory to ``collect_findings`` directly.

Coverage centres on the positional rule — a catch-all handler is flagged
only when a *verdict key* (``reason`` / ``status`` / ``recommended_action``)
is bound to a hardcoded ``server_unreachable`` — plus the two ways a
handler stays clean (binding the key to the classified slug, or peeling
the 404 off with a sibling ``except RommNotFoundError``).
"""

from __future__ import annotations

import importlib.util
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_404_not_unreachable.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_404_not_unreachable", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


def _make_services_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a fake ``py_modules/services/`` tree: ``name -> source``."""
    services_dir = tmp_path / "py_modules" / "services"
    services_dir.mkdir(parents=True)
    for name, source in files.items():
        path = services_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
    return services_dir


def _scan(source: str) -> Any:
    """Scan a single module source, returning its findings."""
    return check.scan_source(Path("mod.py"), textwrap.dedent(source))


# ── Flagged: a verdict key bound to a hardcoded slug ─────────────────────


class TestHardcodedVerdictIsFlagged:
    def test_reason_key_with_error_code_enum(self):
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception as e:
                    return {"success": False, "reason": ErrorCode.SERVER_UNREACHABLE.value, "message": str(e)}
        """)
        assert len(findings) == 1
        assert findings[0].spelling == "ErrorCode.SERVER_UNREACHABLE"
        assert findings[0].function == "f"

    def test_reason_key_without_dot_value(self):
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception:
                    return {"success": False, "reason": ErrorCode.SERVER_UNREACHABLE, "message": "m"}
        """)
        assert len(findings) == 1

    def test_status_discriminant_with_bare_slug(self):
        """The discriminated-status carve-out spells the slug as a literal."""
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception as e:
                    return {"status": "server_unreachable", "message": str(e)}
        """)
        assert len(findings) == 1
        assert findings[0].spelling == '"server_unreachable"'

    def test_recommended_action_with_bare_slug(self):
        """The partial-success carve-out routes on recommended_action."""
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception:
                    return {"recommended_action": "server_unreachable", "server_query_failed": True}
        """)
        assert len(findings) == 1

    def test_classify_error_called_but_verdict_discarded_is_flagged(self):
        """The sharpest form: consults the funnel, then hardcodes anyway.

        A "does the handler call classify_error?" test would wave this
        through — the real defect at ``sync_engine/rollback.py`` before
        #1570.
        """
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception as e:
                    _code, _msg = classify_error(e)
                    return {"success": False, "reason": ErrorCode.SERVER_UNREACHABLE.value, "message": _msg}
        """)
        assert len(findings) == 1

    def test_bare_except_is_catch_all(self):
        findings = _scan("""
            def f():
                try:
                    fetch()
                except:
                    return {"success": False, "reason": ErrorCode.SERVER_UNREACHABLE.value, "message": "m"}
        """)
        assert len(findings) == 1

    def test_base_exception_is_catch_all(self):
        findings = _scan("""
            def f():
                try:
                    fetch()
                except BaseException:
                    return {"success": False, "reason": "server_unreachable", "message": "m"}
        """)
        assert len(findings) == 1

    def test_tuple_clause_containing_exception_is_catch_all(self):
        """``except (ValueError, Exception)`` still swallows everything."""
        findings = _scan("""
            def f():
                try:
                    fetch()
                except (ValueError, Exception):
                    return {"success": False, "reason": "server_unreachable", "message": "m"}
        """)
        assert len(findings) == 1

    def test_nested_dict_value_is_flagged(self):
        """A verdict key bound to a nested literal still hardcodes the slug."""
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception:
                    return {"success": False, "reason": (ErrorCode.SERVER_UNREACHABLE.value), "message": "m"}
        """)
        assert len(findings) == 1


# ── Clean: the shapes the check is steering towards ──────────────────────


class TestCorrectShapesAreClean:
    def test_verdict_key_bound_to_classified_name(self):
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception as e:
                    reason, message = classify_error(e)
                    return {"success": False, "reason": reason, "message": message}
        """)
        assert findings == []

    def test_error_response_helper(self):
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception as e:
                    return error_response(e)
        """)
        assert findings == []

    def test_sibling_not_found_handler_peels_the_404(self):
        """The partial-success shape: no slug to classify, so peel instead."""
        findings = _scan("""
            def f():
                try:
                    fetch()
                except RommNotFoundError:
                    return {"recommended_action": "not_found", "server_query_failed": True}
                except Exception:
                    return {"recommended_action": "server_unreachable", "server_query_failed": True}
        """)
        assert findings == []

    def test_typed_handler_is_never_flagged(self):
        """A deliberate statement about a known type is the goal, not the bug."""
        findings = _scan("""
            def f():
                try:
                    fetch()
                except RommConnectionError:
                    return {"success": False, "reason": ErrorCode.SERVER_UNREACHABLE.value, "message": "offline"}
        """)
        assert findings == []

    def test_slug_in_a_log_line_only(self):
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception as e:
                    log("server_unreachable path taken")
                    return error_response(e)
        """)
        assert findings == []

    def test_comparing_against_the_classified_slug(self):
        """Branching on a classified reason is not a hardcoded verdict."""
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception as e:
                    reason, message = classify_error(e)
                    offline = reason == ErrorCode.SERVER_UNREACHABLE.value
                    return {"success": False, "reason": reason, "message": message, "offline": offline}
        """)
        assert findings == []

    def test_non_verdict_key_is_not_a_verdict(self):
        """The slug under a payload key routes nothing."""
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception as e:
                    reason, message = classify_error(e)
                    return {"success": False, "reason": reason, "message": message, "probe": "server_unreachable"}
        """)
        assert findings == []

    def test_nested_try_peeling_the_404_does_not_flag_the_outer_handler(self):
        """A nested ``try`` is scanned on its own terms, not the enclosing one.

        Attributing the inner dict literals to the outer handler made a nested
        peel read as an unpeeled verdict — a false positive, which would
        wrongly block a future PR.
        """
        findings = _scan("""
            def f():
                try:
                    outer()
                except Exception:
                    try:
                        fetch()
                    except RommNotFoundError:
                        return {"recommended_action": "not_found", "server_query_failed": True}
                    except Exception:
                        return {"recommended_action": "server_unreachable", "server_query_failed": True}
        """)
        assert findings == []

    def test_nested_try_does_not_mask_the_outer_handlers_own_verdict(self):
        """The exclusion is scoped: a verdict in the OUTER body is still found."""
        findings = _scan("""
            def f():
                try:
                    outer()
                except Exception:
                    try:
                        cleanup()
                    except Exception:
                        pass
                    return {"success": False, "reason": ErrorCode.SERVER_UNREACHABLE.value, "message": "m"}
        """)
        assert len(findings) == 1

    def test_handler_with_no_return_dict(self):
        findings = _scan("""
            def f():
                try:
                    fetch()
                except Exception:
                    raise
        """)
        assert findings == []


# ── Tree walking, EXEMPT, and the CLI ────────────────────────────────────


class TestCollectFindings:
    def test_walks_nested_packages(self, tmp_path: Path):
        services_dir = _make_services_tree(
            tmp_path,
            {
                "clean.py": (
                    "def f():\n"
                    "    try:\n"
                    "        fetch()\n"
                    "    except Exception as e:\n"
                    "        return error_response(e)\n"
                ),
                "saves/slots/dirty.py": (
                    "def g():\n"
                    "    try:\n"
                    "        fetch()\n"
                    "    except Exception:\n"
                    '        return {"success": False, "reason": ErrorCode.SERVER_UNREACHABLE.value, "message": "m"}\n'
                ),
            },
        )
        findings = check.collect_findings(services_dir)
        assert len(findings) == 1
        assert findings[0].path.name == "dirty.py"
        assert findings[0].function == "g"

    def test_exempt_module_is_skipped(self, tmp_path: Path):
        dirty = (
            "def f():\n"
            "    try:\n"
            "        fetch()\n"
            "    except Exception:\n"
            '        return {"success": False, "reason": ErrorCode.SERVER_UNREACHABLE.value, "message": "m"}\n'
        )
        services_dir = _make_services_tree(tmp_path, {"steamgrid.py": dirty, "other.py": dirty})
        findings = check.collect_findings(services_dir)
        assert [f.path.name for f in findings] == ["other.py"]

    def test_exempt_entries_carry_a_reason(self):
        """EXEMPT is a dict so every waiver states why, visibly in review."""
        assert check.EXEMPT
        assert all(isinstance(why, str) and why for why in check.EXEMPT.values())

    def test_missing_directory_yields_nothing(self, tmp_path: Path):
        assert check.collect_findings(tmp_path / "nope") == []

    def test_syntax_error_is_skipped(self, tmp_path: Path):
        services_dir = _make_services_tree(tmp_path, {"broken.py": "def f( :\n"})
        assert check.collect_findings(services_dir) == []


class TestCli:
    def test_help_exits_zero(self, capsys):
        assert check.main(["--help"]) == 0
        assert "404-vs-unreachable gate" in capsys.readouterr().out

    def test_report_mode_exits_zero_on_the_real_tree(self, capsys):
        """Report mode is the inventory — it never fails."""
        assert check.main([]) == 0
        assert "SUMMARY" in capsys.readouterr().out

    def test_check_mode_passes_on_the_real_tree(self, capsys):
        """The production services tree must satisfy the invariant."""
        assert check.main(["--check"]) == 0
        assert "OK:" in capsys.readouterr().out
