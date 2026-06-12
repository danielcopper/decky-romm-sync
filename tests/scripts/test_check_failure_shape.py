"""Tests for ``scripts/check_failure_shape.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Fixtures use
``tmp_path`` to lay out a small ``py_modules/services/`` tree the check
walks, monkeypatching the script's ``SERVICES_DIR`` / ``REPO_ROOT``
constants for the duration of the test.

Coverage centres on the required-key rule (a ``success: False`` failure
return must carry both ``reason`` and ``message`` and must not carry
``error`` / ``error_code``) and the two pattern-exempt carve-outs
(discriminated-status unions and partial-success payloads).
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

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_failure_shape.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_failure_shape", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
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


def _wrap_return(body: str) -> str:
    """Wrap a single ``return {...}`` body in a tiny function module."""
    return f"def f():\n    return {body}\n"


def _classify_single(tmp_path: Path, body: str) -> str:
    """Scan a one-return module and return that return's classification."""
    services_dir = _make_services_tree(tmp_path, {"mod.py": _wrap_return(body)})
    findings = check.collect_findings(services_dir)
    assert len(findings) == 1, f"expected exactly one finding, got {findings}"
    return findings[0].classification


# ── Required-key rule: classification ────────────────────────────────────


class TestCanonicalShape:
    def test_reason_and_message_is_canonical(self, tmp_path: Path):
        assert _classify_single(tmp_path, '{"success": False, "reason": "x", "message": "m"}') == check.CANONICAL

    def test_reason_message_plus_extras_is_canonical(self, tmp_path: Path):
        body = '{"success": False, "reason": "x", "message": "m", "synced": 0, "files": []}'
        assert _classify_single(tmp_path, body) == check.CANONICAL

    def test_key_order_does_not_matter(self, tmp_path: Path):
        assert _classify_single(tmp_path, '{"message": "m", "success": False, "reason": "x"}') == check.CANONICAL

    def test_falsy_success_none_still_classified(self, tmp_path: Path):
        # ``success: None`` is falsy — still a failure shape under the rule.
        assert _classify_single(tmp_path, '{"success": None, "reason": "x", "message": "m"}') == check.CANONICAL


class TestForbiddenKeys:
    def test_error_code_key_is_dialect(self, tmp_path: Path):
        body = '{"success": False, "message": "m", "error_code": "x"}'
        assert _classify_single(tmp_path, body) == check.ERROR_CODE_DIALECT

    def test_error_key_is_dialect(self, tmp_path: Path):
        body = '{"success": False, "message": "m", "error": "x"}'
        assert _classify_single(tmp_path, body) == check.ERROR_KEY_DIALECT

    def test_error_code_flagged_even_with_reason_present(self, tmp_path: Path):
        # A forbidden key is a violation regardless of whether reason/message exist.
        body = '{"success": False, "reason": "x", "message": "m", "error_code": "x"}'
        assert _classify_single(tmp_path, body) == check.ERROR_CODE_DIALECT


class TestAdHoc:
    def test_message_only_no_reason_is_ad_hoc(self, tmp_path: Path):
        assert _classify_single(tmp_path, '{"success": False, "message": "m"}') == check.AD_HOC

    def test_reason_only_no_message_is_ad_hoc(self, tmp_path: Path):
        assert _classify_single(tmp_path, '{"success": False, "reason": "x"}') == check.AD_HOC

    def test_success_only_is_ad_hoc(self, tmp_path: Path):
        assert _classify_single(tmp_path, '{"success": False}') == check.AD_HOC

    def test_extras_only_no_reason_no_message_is_ad_hoc(self, tmp_path: Path):
        assert _classify_single(tmp_path, '{"success": False, "synced": 0, "errors": []}') == check.AD_HOC


# ── Carve-outs (pattern-exempt) ──────────────────────────────────────────


class TestDiscriminatedStatusCarveOut:
    def test_status_without_success_is_carve_out(self, tmp_path: Path):
        body = '{"status": "server_unreachable", "message": "m"}'
        assert _classify_single(tmp_path, body) == check.CARVE_OUT_CANDIDATE

    def test_status_with_only_versions_is_carve_out(self, tmp_path: Path):
        assert _classify_single(tmp_path, '{"status": "ok", "versions": []}') == check.CARVE_OUT_CANDIDATE

    def test_status_with_success_false_is_not_carve_out(self, tmp_path: Path):
        # A dict carrying BOTH status and a falsy success is not the union shape —
        # it falls through to the required-key rule (AD_HOC here, no reason/message).
        assert _classify_single(tmp_path, '{"success": False, "status": "x"}') == check.AD_HOC


class TestPartialSuccessCarveOut:
    def test_server_query_failed_flag_is_carve_out(self, tmp_path: Path):
        body = '{"total_seconds": 0, "session_count": 0, "server_query_failed": True}'
        assert _classify_single(tmp_path, body) == check.CARVE_OUT_CANDIDATE

    def test_recommended_action_flag_is_carve_out(self, tmp_path: Path):
        body = '{"has_local_saves": False, "recommended_action": "server_unreachable"}'
        assert _classify_single(tmp_path, body) == check.CARVE_OUT_CANDIDATE

    def test_partial_flag_wins_over_falsy_success(self, tmp_path: Path):
        # Partial-success payloads may carry success: False alongside the flag;
        # the carve-out still applies (the flag is checked before the dialects).
        body = '{"success": False, "server_query_failed": True, "data": []}'
        assert _classify_single(tmp_path, body) == check.CARVE_OUT_CANDIDATE


# ── Non-failure returns are ignored ──────────────────────────────────────


class TestNonFailureReturnsIgnored:
    def test_success_true_dict_not_a_finding(self, tmp_path: Path):
        services_dir = _make_services_tree(tmp_path, {"mod.py": _wrap_return('{"success": True, "message": "ok"}')})
        assert check.collect_findings(services_dir) == []

    def test_non_dict_return_not_a_finding(self, tmp_path: Path):
        services_dir = _make_services_tree(tmp_path, {"mod.py": "def f():\n    return []\n"})
        assert check.collect_findings(services_dir) == []

    def test_bare_return_not_a_finding(self, tmp_path: Path):
        services_dir = _make_services_tree(tmp_path, {"mod.py": "def f():\n    return\n"})
        assert check.collect_findings(services_dir) == []


# ── Dynamic-key heuristic (spread / computed keys) ───────────────────────


class TestDynamicKeys:
    def test_spread_failure_dict_flagged_ad_hoc(self, tmp_path: Path):
        # A ``**base`` spread hides the full key set on a falsy-success dict —
        # flagged AD_HOC for manual review (never silently passed).
        body = '{**base, "success": False}'
        services_dir = _make_services_tree(tmp_path, {"mod.py": f"def f(base):\n    return {body}\n"})
        findings = check.collect_findings(services_dir)
        assert len(findings) == 1
        assert findings[0].classification == check.AD_HOC

    def test_spread_status_union_flagged_carve_out(self, tmp_path: Path):
        body = '{**base, "status": "ok"}'
        services_dir = _make_services_tree(tmp_path, {"mod.py": f"def f(base):\n    return {body}\n"})
        findings = check.collect_findings(services_dir)
        assert len(findings) == 1
        assert findings[0].classification == check.CARVE_OUT_CANDIDATE


# ── Robustness ───────────────────────────────────────────────────────────


class TestScanRobustness:
    def test_syntax_error_file_skipped(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        services_dir = _make_services_tree(
            tmp_path,
            {
                "broken.py": "def(\n",
                "good.py": _wrap_return('{"success": False, "message": "m"}'),
            },
        )
        findings = check.collect_findings(services_dir)
        assert len(findings) == 1
        assert findings[0].rel.endswith("good.py")

    def test_missing_services_dir_returns_empty(self, tmp_path: Path):
        assert check.collect_findings(tmp_path / "nope") == []

    def test_nested_package_files_scanned(self, tmp_path: Path):
        services_dir = _make_services_tree(
            tmp_path,
            {"saves/sync_engine/devices.py": _wrap_return('{"success": False, "error": "list_failed"}')},
        )
        findings = check.collect_findings(services_dir)
        assert len(findings) == 1
        assert findings[0].classification == check.ERROR_KEY_DIALECT


# ── main() entry point ───────────────────────────────────────────────────


class TestMainEntryPoint:
    def test_help_flag_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        rc = check.main(["--help"])
        assert rc == 0
        assert "Failure-shape dialect gate" in capsys.readouterr().out

    def test_short_help_flag_returns_zero(self):
        assert check.main(["-h"]) == 0

    def test_real_repo_report_is_clean(self, capsys: pytest.CaptureFixture[str]):
        # Report mode never fails; it prints the inventory + summary.
        rc = check.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert "=== SUMMARY ===" in out

    def test_real_repo_enforce_is_clean(self, capsys: pytest.CaptureFixture[str]):
        # The real services/ tree must be fully collapsed onto the canonical
        # shape — enforce mode passes (the whole point of the migration).
        rc = check.main(["--check"])
        assert rc == 0
        assert "OK:" in capsys.readouterr().out

    def test_enforce_fails_on_dialect(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        services_dir = _make_services_tree(
            tmp_path,
            {"mod.py": _wrap_return('{"success": False, "message": "m", "error_code": "x"}')},
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)
        rc = check.main(["--check"])
        assert rc == 1
        out = capsys.readouterr().out
        assert "ERROR:" in out
        assert "error_code" not in out or "mod.py" in out

    def test_enforce_fails_on_ad_hoc(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        services_dir = _make_services_tree(
            tmp_path,
            {"mod.py": _wrap_return('{"success": False, "message": "m"}')},
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)
        rc = check.main(["--check"])
        assert rc == 1
        assert "ERROR:" in capsys.readouterr().out

    def test_enforce_passes_on_canonical_and_carve_outs(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        services_dir = _make_services_tree(
            tmp_path,
            {
                "canonical.py": _wrap_return('{"success": False, "reason": "x", "message": "m"}'),
                "status.py": _wrap_return('{"status": "server_unreachable", "message": "m"}'),
                "partial.py": _wrap_return('{"data": [], "server_query_failed": True}'),
                "ok.py": _wrap_return('{"success": True, "message": "ok"}'),
            },
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)
        rc = check.main(["--check"])
        assert rc == 0
        assert "OK:" in capsys.readouterr().out

    def test_report_groups_and_summarises(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        services_dir = _make_services_tree(
            tmp_path,
            {
                "a.py": _wrap_return('{"success": False, "reason": "x", "message": "m"}'),
                "b.py": _wrap_return('{"success": False, "error": "x"}'),
            },
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)
        rc = check.main([])
        assert rc == 0
        out = capsys.readouterr().out
        assert f"=== {check.CANONICAL} (1) ===" in out
        assert f"=== {check.ERROR_KEY_DIALECT} (1) ===" in out
        assert "TOTAL: 2" in out
