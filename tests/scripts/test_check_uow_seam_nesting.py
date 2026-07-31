"""Tests for ``scripts/check_uow_seam_nesting.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Most cases drive the
``scan_source`` core function with small source snippets; the tmp-path
fixture exercises the directory walk + the ``main`` entry point,
monkeypatching the script's ``REPO_ROOT`` / ``SERVICES_DIR`` constants.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from types import ModuleType

    import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_uow_seam_nesting.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_uow_seam_nesting", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


class TestScanSourceViolations:
    def test_seam_call_inside_open_uow_is_flagged(self):
        # The pre-#1283 shape: resolve the active core INSIDE the open write UoW.
        findings = check.scan_source(
            "class S:\n"
            "    def io(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            rom = uow.roms.get(rom_id)\n"
            "            emu = self._active_core.active_emulator_for_rom(rom_id)\n"
            "        return emu\n",
            "svc.py",
        )
        assert len(findings) == 1
        assert "svc.py:5" in findings[0]
        assert "active_emulator_for_rom" in findings[0]

    def test_active_core_for_rom_inside_uow_is_flagged(self):
        findings = check.scan_source(
            "class S:\n"
            "    def io(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            core, _ = self._active_core.active_core_for_rom(rom_id)\n"
            "        return core\n",
            "svc.py",
        )
        assert len(findings) == 1
        assert "active_core_for_rom" in findings[0]

    def test_relaunch_resolver_seam_inside_uow_is_flagged(self):
        findings = check.scan_source(
            "class S:\n"
            "    def go(self):\n"
            "        with self._uow_factory() as uow:\n"
            "            items = self._relaunch.installed_relaunch_items()\n"
            "        return items\n",
            "svc.py",
        )
        assert len(findings) == 1
        assert "installed_relaunch_items" in findings[0]

    def test_relaunch_item_for_rom_inside_uow_is_flagged(self):
        findings = check.scan_source(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            item = self._relaunch.relaunch_item_for_rom(rom_id)\n"
            "        return item\n",
            "svc.py",
        )
        assert len(findings) == 1
        assert "relaunch_item_for_rom" in findings[0]

    def test_nested_bare_factory_open_is_flagged(self):
        # The #1155 migration shape: a second ``with self._uow_factory()`` nested
        # inside the first — the inner open deadlocks on the outer's write lock.
        findings = check.scan_source(
            "class S:\n"
            "    def run(self):\n"
            "        with self._uow_factory() as uow:\n"
            "            for rom in uow.roms.iter_all():\n"
            "                with self._uow_factory() as uow2:\n"
            "                    uow2.roms.save(rom)\n",
            "svc.py",
        )
        assert len(findings) == 1
        assert "nested UoW open" in findings[0]

    def test_seam_inside_try_within_uow_is_flagged(self):
        # Control-flow nesting (try/except) inside the UoW is still inside it.
        findings = check.scan_source(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            try:\n"
            "                a = self._active_core.active_core_for_rom(rom_id)\n"
            "            except Exception:\n"
            "                a = None\n"
            "        return a\n",
            "svc.py",
        )
        assert len(findings) == 1
        assert "active_core_for_rom" in findings[0]

    def test_seam_inside_nested_uow_still_flagged_once(self):
        # A seam reached inside a doubly-nested UoW is one finding for the seam
        # plus one for the nested open — each distinct node flagged exactly once.
        findings = check.scan_source(
            "class S:\n"
            "    def run(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            with self._uow_factory() as uow2:\n"
            "                core, _ = self._active_core.active_core_for_rom(rom_id)\n"
            "        return core\n",
            "svc.py",
        )
        assert len(findings) == 2
        assert any("nested UoW open" in f for f in findings)
        assert any("active_core_for_rom" in f for f in findings)

    def test_config_held_factory_name_also_opens_uow(self):
        # The opener is matched by the ``uow_factory`` suffix, so a factory held
        # on config (``config.uow_factory()``) is recognised as an open too.
        findings = check.scan_source(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with config.uow_factory() as uow:\n"
            "            core, _ = self._active_core.active_core_for_rom(rom_id)\n"
            "        return core\n",
            "svc.py",
        )
        assert len(findings) == 1
        assert "active_core_for_rom" in findings[0]


class TestScanSourceClean:
    def test_seam_after_uow_closes_is_clean(self):
        # The canonical fix: snapshot inside the UoW, resolve after it closes.
        findings = check.scan_source(
            "class S:\n"
            "    def io(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            rom = uow.roms.get(rom_id)\n"
            "        emu = self._active_core.active_emulator_for_rom(rom_id)\n"
            "        return emu\n",
            "svc.py",
        )
        assert findings == []

    def test_seam_never_inside_any_uow_is_clean(self):
        findings = check.scan_source(
            "class S:\n"
            "    def resolve(self, rom_id):\n"
            "        core, _ = self._active_core.active_core_for_rom(rom_id)\n"
            "        return core\n",
            "svc.py",
        )
        assert findings == []

    def test_helper_defined_inside_uow_but_not_called_is_clean(self):
        # A nested def resets the scope: a helper *defined* inside a UoW that
        # calls the seam is not a call inside the UoW.
        findings = check.scan_source(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            def helper():\n"
            "                return self._active_core.active_core_for_rom(rom_id)\n"
            "            uow.roms.touch(rom_id)\n"
            "        return helper\n",
            "svc.py",
        )
        assert findings == []

    def test_lambda_inside_uow_is_clean(self):
        findings = check.scan_source(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            fn = lambda: self._active_core.active_core_for_rom(rom_id)\n"
            "        return fn\n",
            "svc.py",
        )
        assert findings == []

    def test_escape_hatch_suppresses_finding(self):
        findings = check.scan_source(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            x = self._active_core.active_core_for_rom(rom_id)  # pragma: no uow-check\n"
            "        return x\n",
            "svc.py",
        )
        assert findings == []

    def test_unrelated_call_inside_uow_is_clean(self):
        findings = check.scan_source(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            rom = uow.roms.get(rom_id)\n"
            "            self._logger.info('got %s', rom)\n"
            "        return rom\n",
            "svc.py",
        )
        assert findings == []

    def test_outer_factory_open_itself_is_not_flagged(self):
        # The ``with self._uow_factory()`` opener is not a nested open — only a
        # factory call reached while already inside a UoW is.
        findings = check.scan_source(
            "class S:\n    def go(self):\n        with self._uow_factory() as uow:\n            uow.roms.touch(1)\n",
            "svc.py",
        )
        assert findings == []

    def test_syntax_error_is_swallowed(self):
        assert check.scan_source("def (:\n", "broken.py") == []


class TestFindViolationsWalk:
    def test_walk_finds_and_relativises(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        services_dir = tmp_path / "py_modules" / "services"
        services_dir.mkdir(parents=True)
        (services_dir / "clean.py").write_text(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            rom = uow.roms.get(rom_id)\n"
            "        return self._active_core.active_core_for_rom(rom_id)\n",
            encoding="utf-8",
        )
        (services_dir / "bad.py").write_text(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            return self._active_core.active_core_for_rom(rom_id)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)

        findings = check.find_violations(services_dir)
        assert len(findings) == 1
        assert findings[0].startswith("py_modules/services/bad.py:")

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        assert check.find_violations(tmp_path / "does-not-exist") == []


class TestMainEntryPoint:
    def test_help_flag_returns_zero(self, capsys):
        rc = check.main(["--help"])
        assert rc == 0
        assert "UoW-seam nesting ban" in capsys.readouterr().out

    def test_short_help_flag_returns_zero(self):
        assert check.main(["-h"]) == 0

    def test_real_repo_run_is_clean(self, capsys):
        # The four motivating sites are fixed; the real services/ tree must pass.
        rc = check.main([])
        assert rc == 0
        assert "OK:" in capsys.readouterr().out

    def test_main_reports_and_returns_one_on_violations(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
        services_dir = tmp_path / "py_modules" / "services"
        services_dir.mkdir(parents=True)
        (services_dir / "bad.py").write_text(
            "class S:\n"
            "    def go(self, rom_id):\n"
            "        with self._uow_factory() as uow:\n"
            "            return self._active_core.active_core_for_rom(rom_id)\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)

        rc = check.main([])
        assert rc == 1
        out = capsys.readouterr().out
        assert "active_core_for_rom" in out
        assert "ERROR:" in out
