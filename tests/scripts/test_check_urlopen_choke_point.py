"""Tests for ``scripts/check_urlopen_choke_point.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Each case writes a small
stand-in transport module under ``tmp_path`` and runs the real AST walk over it.

Coverage centres on the confinement rule (``urllib.request.urlopen`` may be
called only inside ``_urlopen``) and on the boundaries that decide whether a
call site counts: the dotted name being matched, and a mere reference to the
function that is not a call.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_urlopen_choke_point.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_urlopen_choke_point", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()

_CHOKE_POINT = """
import urllib.request


class RommHttpAdapter:
    def _urlopen(self, req, *, timeout):
        return urllib.request.urlopen(req, context=self.ssl_context(), timeout=timeout)
"""


@pytest.fixture
def run_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Yield a helper that writes *source* as the transport module and runs the real check."""

    def _run(source: str) -> list[str]:
        transport = tmp_path / "py_modules" / "adapters" / "romm" / "http.py"
        transport.parent.mkdir(parents=True, exist_ok=True)
        transport.write_text(source, encoding="utf-8")
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "TRANSPORT", transport)
        return check.find_violations()

    return _run


class TestChokePointHolds:
    def test_the_choke_point_itself_is_allowed(self, run_check):
        assert run_check(_CHOKE_POINT) == []

    def test_a_method_routed_through_the_choke_point_is_allowed(self, run_check):
        source = (
            _CHOKE_POINT
            + """
    def request(self, path):
        req = urllib.request.Request(path)
        with self._urlopen(req, timeout=30) as resp:
            return resp.read()
"""
        )
        assert run_check(source) == []

    def test_a_module_with_no_urlopen_at_all_is_allowed(self, run_check):
        assert run_check("import urllib.request\n\n\nclass RommHttpAdapter:\n    pass\n") == []


class TestChokePointBypassed:
    def test_a_new_request_method_calling_urlopen_is_reported(self, run_check):
        source = (
            _CHOKE_POINT
            + """
    def request_rogue(self, path):
        req = urllib.request.Request(path)
        return urllib.request.urlopen(req, timeout=30)
"""
        )
        findings = run_check(source)
        assert len(findings) == 1
        assert "outside _urlopen()" in findings[0]
        offending_line = source.splitlines().index("        return urllib.request.urlopen(req, timeout=30)") + 1
        assert f"py_modules/adapters/romm/http.py:{offending_line}:" in findings[0]

    def test_every_offending_site_is_reported_not_just_the_first(self, run_check):
        source = (
            _CHOKE_POINT
            + """
    def a(self, req):
        return urllib.request.urlopen(req, timeout=30)

    def b(self, req):
        return urllib.request.urlopen(req, timeout=30)
"""
        )
        assert len(run_check(source)) == 2

    def test_a_module_level_call_outside_any_function_is_reported(self, run_check):
        source = _CHOKE_POINT + "\n\n_probe = urllib.request.urlopen('http://example.invalid')\n"
        assert len(run_check(source)) == 1

    def test_a_call_in_another_class_is_reported(self, run_check):
        source = """
import urllib.request


class Other:
    def fetch(self, req):
        return urllib.request.urlopen(req)
"""
        assert len(run_check(source)) == 1

    def test_a_nested_function_named_urlopen_does_not_launder_a_call(self, run_check):
        """Exemption is the class/method PAIR, not the bare name.

        Matching on the name alone would let any method host a local helper
        called ``_urlopen`` and exempt its own call — a request path that opens
        its own connection while reading as if it went through the choke point.
        """
        source = (
            _CHOKE_POINT
            + """
    def rogue(self, req):
        def _urlopen(r):
            return urllib.request.urlopen(r)

        return _urlopen(req)
"""
        )
        assert len(run_check(source)) == 1

    def test_a_same_named_method_on_another_class_does_not_launder_a_call(self, run_check):
        source = (
            _CHOKE_POINT
            + """

class SgdbAdapter:
    def _urlopen(self, req):
        return urllib.request.urlopen(req)
"""
        )
        assert len(run_check(source)) == 1


class TestMatchBoundaries:
    def test_a_different_dotted_call_is_not_matched(self, run_check):
        source = (
            _CHOKE_POINT
            + """
    def build(self, path):
        return urllib.request.Request(path)
"""
        )
        assert run_check(source) == []

    def test_a_bare_reference_without_a_call_is_not_matched(self, run_check):
        """Passing the function as a value is not a call site — the check walks calls only."""
        source = (
            _CHOKE_POINT
            + """
    def opener(self):
        return urllib.request.urlopen
"""
        )
        assert run_check(source) == []
