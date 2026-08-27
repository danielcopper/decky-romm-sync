"""Tests for ``scripts/check_read_only_module.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Fixtures write a single
module under ``tmp_path`` and point the check's declaration table at it, so the
scan runs exactly as it does in CI.

Coverage centres on the rule (a declared module calls repository reads only),
the read/write split as ``services/protocols/repositories.py`` actually shapes
it, the receiver-shaped narrowing that keeps unrelated two-deep calls out, and
the documented blind spots — a gate whose advertised reach outruns its real one
is worse than none, because the register quotes the advert.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_read_only_module.py"

_MODULE = "py_modules/services/library/registry_queries.py"
_REASON = "it only ever reads"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_read_only_module", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


@pytest.fixture
def scan(tmp_path: Path):
    """Yield a helper that writes one declared module's source and scans it."""

    def _run(source: str, *, module: str = _MODULE) -> list[str]:
        path = tmp_path / module
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        return check.find_violations({module: _REASON}, root=tmp_path)

    return _run


class TestWritesAreFlagged:
    def test_flags_a_repository_save(self, scan):
        findings = scan("def go(self):\n    with self._uow_factory() as uow:\n        uow.roms.save(rom)\n")
        assert len(findings) == 1
        assert "roms.save(...)" in findings[0]
        assert _REASON in findings[0]  # the message carries WHY, not only the rule

    def test_flags_a_delete_on_a_stamp_repository(self, scan):
        # The exact write that deliberately stayed in the orchestrator.
        findings = scan("def go(self, slug):\n    self._uow.platform_sync_state.delete(slug)\n")
        assert len(findings) == 1
        assert "platform_sync_state.delete(...)" in findings[0]

    def test_flags_setters_and_clears_and_replace_all(self, scan):
        findings = scan(
            "def go(uow):\n"
            "    uow.roms.set_applied_launch_options(1, '')\n"
            "    uow.roms.clear_all_applied_launch_options()\n"
            "    uow.platform_sync_state.clear()\n"
            "    uow.firmware_cache.replace_all([])\n"
            "    uow.kv_config.set('k', 'v')\n"
        )
        assert len(findings) == 5

    def test_reports_every_write_site_separately(self, scan):
        findings = scan("def go(uow):\n    uow.roms.save(a)\n    uow.roms.save(b)\n")
        assert len(findings) == 2
        assert findings[0] != findings[1]

    def test_a_missing_declared_module_is_a_finding(self, tmp_path: Path):
        # A stale entry must fail rather than pass vacuously.
        findings = check.find_violations({"py_modules/nope.py": _REASON}, root=tmp_path)
        assert len(findings) == 1
        assert "could not be read" in findings[0]


class TestReadsAreNotFlagged:
    def test_the_real_module_is_clean(self):
        # The production declaration, scanned as CI scans it.
        assert check.find_violations() == []

    def test_every_read_shape_passes(self, scan):
        findings = scan(
            "def go(uow):\n"
            "    uow.roms.get(1)\n"
            "    uow.roms.get_by_app_id(2)\n"
            "    uow.roms.get_all_emulator_overrides()\n"
            "    uow.roms.iter_all()\n"
            "    uow.roms.iter_by_platform('n64')\n"
            "    uow.roms.iter_by_group_key('k')\n"
            "    uow.roms.count()\n"
            "    uow.rom_metadata.iter_page(0, 10)\n"
            "    uow.sync_runs.get_latest_completed()\n"
            "    uow.sync_runs.get_latest_terminal()\n"
            "    uow.sync_runs.get_running()\n"
            "    uow.firmware_cache.get_cache_epoch()\n"
            "    uow.playtime.iter_pending_sessions(5)\n"
            "    uow.playtime.rom_ids_with_pending_device('d')\n"
            "    uow.platform_sync_state.get('n64')\n"
        )
        assert findings == []

    def test_non_repository_calls_are_ignored(self, scan):
        # Same two-deep shape, receiver is not a repository — the narrowing that
        # keeps the gate off everything else a service touches.
        findings = scan(
            "def go(self, uow):\n"
            "    self._logger.save(1)\n"
            "    self._artwork.download_artwork(x)\n"
            "    uow.commit()\n"
            "    select_stale_removals(a, b)\n"
        )
        assert findings == []

    def test_count_is_exact_not_a_prefix(self, scan):
        # ``count`` reads; a name merely starting with it must not inherit that.
        assert scan("def go(uow):\n    uow.roms.count()\n") == []
        findings = scan("def go(uow):\n    uow.roms.count_and_prune()\n")
        assert len(findings) == 1


class TestDocumentedBlindSpots:
    def test_an_aliased_repository_handle_escapes(self, scan):
        # ``repo = uow.roms`` flattens the two-attribute shape the scan matches.
        findings = scan("def go(uow):\n    repo = uow.roms\n    repo.save(rom)\n")
        assert findings == []

    def test_a_getattr_reached_repository_escapes(self, scan):
        findings = scan("def go(uow):\n    getattr(uow, 'roms').save(rom)\n")
        assert findings == []

    def test_a_write_behind_a_helper_escapes(self, scan):
        # Only calls written in the declared module are inspected.
        findings = scan("from helpers import persist\n\ndef go(uow):\n    persist(uow, rom)\n")
        assert findings == []

    def test_an_unknown_repository_name_escapes(self, scan):
        findings = scan("def go(uow):\n    uow.future_repo.save(rom)\n")
        assert findings == []


class TestTables:
    def test_repository_attrs_match_the_unit_of_work_protocol(self):
        # The scan's whole precision rests on this list being the UoW's own.
        import ast

        source = (Path(__file__).resolve().parents[2] / "py_modules/services/protocols/uow.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        uow_class = next(
            node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "UnitOfWork"
        )
        properties = {
            node.name
            for node in uow_class.body
            if isinstance(node, ast.FunctionDef)
            and any(isinstance(d, ast.Name) and d.id == "property" for d in node.decorator_list)
        }
        assert properties == set(check.REPOSITORY_ATTRS)

    def test_no_write_name_from_the_repository_protocols_reads_as_a_read(self):
        writes = {
            "save",
            "delete",
            "clear",
            "clear_all_applied_launch_options",
            "replace_all",
            "set",
            "set_emulator_override",
            "set_selected_disc",
            "set_applied_launch_options",
            "set_fs_size_bytes",
        }
        assert not any(check._is_read(name) for name in writes)


class TestMainEntryPoint:
    def test_help_flag_returns_zero(self, capsys):
        rc = check.main(["--help"])
        assert rc == 0
        assert "Read-only module gate" in capsys.readouterr().out

    def test_real_repo_run_is_clean(self, capsys):
        rc = check.main([])
        assert rc == 0
        assert "OK:" in capsys.readouterr().out

    def test_main_reports_and_returns_one_on_a_planted_write(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ):
        path = tmp_path / _MODULE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def go(uow):\n    uow.roms.save(rom)\n", encoding="utf-8")
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)

        rc = check.main([])
        assert rc == 1
        captured = capsys.readouterr()
        assert "roms.save(...)" in captured.out
        assert "ERROR:" in captured.out
