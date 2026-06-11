"""Tests for ``scripts/check_service_independence_contract.py``.

Loaded via ``importlib`` because ``scripts/`` is not on ``sys.path`` (and is
excluded from ruff/basedpyright). Fixtures build a fake ``py_modules/services/``
tree and a fake ``.importlinter`` under ``tmp_path`` and monkeypatch the script's
module-level path constants.
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

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_service_independence_contract.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_service_independence_contract", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


def _make_services_tree(tmp_path: Path, *, files: list[str], packages: list[str]) -> Path:
    """Build a fake ``py_modules/services/`` tree: ``files`` -> ``<name>.py``, ``packages`` -> ``<dir>/__init__.py``."""
    services_dir = tmp_path / "py_modules" / "services"
    services_dir.mkdir(parents=True)
    (services_dir / "__init__.py").write_text("", encoding="utf-8")
    for name in files:
        (services_dir / f"{name}.py").write_text("", encoding="utf-8")
    for pkg in packages:
        pkg_dir = services_dir / pkg
        pkg_dir.mkdir()
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")
    return services_dir


def _write_importlinter(tmp_path: Path, modules: list[str]) -> Path:
    """Write a minimal ``.importlinter`` carrying just the service-independence contract."""
    listing = "\n".join(f"    {m}" for m in modules)
    content = (
        textwrap.dedent(
            """\
            [importlinter]
            root_packages =
                services

            [importlinter:contract:service-independence]
            name = Services must not import other services
            type = independence
            modules =
            """
        )
        + listing
        + "\n"
    )
    path = tmp_path / ".importlinter"
    path.write_text(content, encoding="utf-8")
    return path


class TestDeriveServices:
    def test_top_level_modules_and_packages(self, tmp_path: Path):
        services_dir = _make_services_tree(tmp_path, files=["connection", "settings"], packages=["saves", "library"])
        assert check.derive_services(services_dir) == {
            "services.connection",
            "services.settings",
            "services.saves",
            "services.library",
        }

    def test_init_excluded(self, tmp_path: Path):
        services_dir = _make_services_tree(tmp_path, files=["connection"], packages=[])
        assert "services.__init__" not in check.derive_services(services_dir)

    def test_protocols_package_excluded(self, tmp_path: Path):
        services_dir = _make_services_tree(tmp_path, files=["connection"], packages=["protocols", "saves"])
        result = check.derive_services(services_dir)
        assert "services.protocols" not in result
        assert result == {"services.connection", "services.saves"}

    def test_subpackage_counts_as_one_entry(self, tmp_path: Path):
        # Files INSIDE a sub-package must not become their own entries.
        services_dir = _make_services_tree(tmp_path, files=[], packages=["saves"])
        (services_dir / "saves" / "versions.py").write_text("", encoding="utf-8")
        (services_dir / "saves" / "sync_engine").mkdir()
        (services_dir / "saves" / "sync_engine" / "__init__.py").write_text("", encoding="utf-8")
        assert check.derive_services(services_dir) == {"services.saves"}

    def test_non_package_dir_ignored(self, tmp_path: Path):
        # A directory without __init__.py (e.g. __pycache__) is not a service.
        services_dir = _make_services_tree(tmp_path, files=["connection"], packages=[])
        (services_dir / "__pycache__").mkdir()
        assert check.derive_services(services_dir) == {"services.connection"}

    def test_missing_dir_returns_empty(self, tmp_path: Path):
        assert check.derive_services(tmp_path / "does_not_exist") == set()


class TestParseContractModules:
    def test_reads_multiline_list(self, tmp_path: Path):
        path = _write_importlinter(tmp_path, ["services.connection", "services.saves", "services.library"])
        assert check.parse_contract_modules(path) == {
            "services.connection",
            "services.saves",
            "services.library",
        }

    def test_missing_section_returns_empty(self, tmp_path: Path):
        path = tmp_path / ".importlinter"
        path.write_text("[importlinter]\nroot_packages =\n    services\n", encoding="utf-8")
        assert check.parse_contract_modules(path) == set()


class TestFindDiscrepancies:
    def test_in_sync_no_findings(self):
        on_disk = {"services.a", "services.b"}
        assert check.find_discrepancies(on_disk, {"services.a", "services.b"}, set()) == []

    def test_missing_from_contract_flagged(self):
        findings = check.find_discrepancies({"services.a", "services.b"}, {"services.a"}, set())
        assert len(findings) == 1
        assert "services.b" in findings[0]
        assert "not enrolled" in findings[0]

    def test_stale_contract_entry_flagged(self):
        findings = check.find_discrepancies({"services.a"}, {"services.a", "services.gone"}, set())
        assert len(findings) == 1
        assert "services.gone" in findings[0]
        assert "no such service" in findings[0]

    def test_exempt_service_not_flagged_as_missing(self):
        # Service on disk, absent from contract, but EXEMPT -> not a discrepancy.
        findings = check.find_discrepancies({"services.a", "services.b"}, {"services.a"}, {"services.b"})
        assert findings == []

    def test_exempt_and_listed_is_contradiction(self):
        findings = check.find_discrepancies({"services.a"}, {"services.a"}, {"services.a"})
        assert len(findings) == 1
        assert "services.a" in findings[0]
        assert "EXEMPT" in findings[0]


class TestMainEntryPoint:
    def test_help_flag_returns_zero(self, capsys: pytest.CaptureFixture[str]):
        rc = check.main(["--help"])
        assert rc == 0
        assert "Service-independence contract" in capsys.readouterr().out

    def test_short_help_flag_returns_zero(self):
        assert check.main(["-h"]) == 0

    def test_real_repo_run_is_clean(self, capsys: pytest.CaptureFixture[str]):
        # Locks the actual .importlinter service-independence contract in sync with
        # py_modules/services/. If this fails, a service was added/renamed without
        # updating the contract (the whole point of the check).
        rc = check.main([])
        assert rc == 0
        assert "OK:" in capsys.readouterr().out

    def test_drift_reports_and_returns_one(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ):
        services_dir = _make_services_tree(tmp_path, files=["connection", "settings"], packages=["saves"])
        importlinter_path = _write_importlinter(tmp_path, ["services.connection", "services.saves"])
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)
        monkeypatch.setattr(check, "IMPORTLINTER_PATH", importlinter_path)
        monkeypatch.setattr(check, "EXEMPT", set())
        rc = check.main([])
        assert rc == 1
        out = capsys.readouterr().out
        assert "services.settings" in out
        assert "ERROR:" in out

    def test_in_sync_fake_repo_returns_zero(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        services_dir = _make_services_tree(tmp_path, files=["connection"], packages=["saves"])
        importlinter_path = _write_importlinter(tmp_path, ["services.connection", "services.saves"])
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)
        monkeypatch.setattr(check, "IMPORTLINTER_PATH", importlinter_path)
        monkeypatch.setattr(check, "EXEMPT", set())
        assert check.main([]) == 0
