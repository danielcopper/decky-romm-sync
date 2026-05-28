"""Tests for ``scripts/check_aggregate_field_assignment.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Fixtures use
``tmp_path`` to lay out small domain/ + services/ trees that the check
walks, monkeypatching the script's ``DOMAIN_DIR`` / ``SERVICES_DIR`` /
``REPO_ROOT`` constants for the duration of the test.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_aggregate_field_assignment.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_aggregate_field_assignment", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()


def _make_fake_tree(
    tmp_path: Path,
    *,
    domain_files: dict[str, str] | None = None,
    services_files: dict[str, str] | None = None,
) -> tuple[Path, Path]:
    """Build a fake ``py_modules/{domain,services}/`` tree under ``tmp_path``."""
    domain_dir = tmp_path / "py_modules" / "domain"
    services_dir = tmp_path / "py_modules" / "services"
    domain_dir.mkdir(parents=True)
    services_dir.mkdir(parents=True)
    for name, source in (domain_files or {}).items():
        (domain_dir / name).write_text(source, encoding="utf-8")
    for name, source in (services_files or {}).items():
        path = services_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
    return domain_dir, services_dir


@pytest.fixture
def patched_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Yield a helper that retargets the check at a tmp-path layer + runs it."""

    def _run(
        *,
        domain_files: dict[str, str] | None = None,
        services_files: dict[str, str] | None = None,
    ) -> tuple[set[str], list[str]]:
        domain_dir, services_dir = _make_fake_tree(tmp_path, domain_files=domain_files, services_files=services_files)
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "DOMAIN_DIR", domain_dir)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)
        names = check.collect_aggregate_class_names(domain_dir)
        findings = check.find_violations(services_dir, names)
        return names, findings

    return _run


class TestCollectAggregateClassNames:
    def test_collects_classes_decorated_with_bare_name(self, patched_check):
        names, _ = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
        )
        assert names == {"Rom"}

    def test_ignores_classes_without_decorator(self, patched_check):
        names, _ = patched_check(
            domain_files={
                "save_state.py": "class SaveSyncState:\n    pass\n",
            },
        )
        assert names == set()

    def test_mixed_decorated_and_undecorated(self, patched_check):
        names, _ = patched_check(
            domain_files={
                "mixed.py": (
                    "from domain._aggregate import cosmic_aggregate\n"
                    "@cosmic_aggregate\n"
                    "class Rom:\n"
                    "    pass\n"
                    "class LegacyContainer:\n"
                    "    pass\n"
                ),
            },
        )
        assert names == {"Rom"}

    def test_handles_syntax_error_files(self, patched_check):
        # Bad syntax shouldn't crash the walk
        names, _ = patched_check(
            domain_files={
                "broken.py": "def(\n",
                "good.py": (
                    "from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Platform:\n    pass\n"
                ),
            },
        )
        assert names == {"Platform"}


class TestFindViolationsHappyPath:
    def test_flags_direct_field_assignment(self, patched_check):
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "library.py": ("def update(rom):\n    rom.cover_path = '/x'\n"),
            },
        )
        assert len(findings) == 1
        assert "library.py" in findings[0]
        assert "rom.cover_path" in findings[0]
        assert "Rom is @cosmic_aggregate" in findings[0]

    def test_clean_file_with_no_violations(self, patched_check):
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "clean.py": ("def update(rom):\n    return rom.cover_path\n"),
            },
        )
        assert findings == []

    def test_undecorated_container_not_flagged(self, patched_check):
        _, findings = patched_check(
            domain_files={
                "save_state.py": "class SaveSyncState:\n    pass\n",
            },
            services_files={
                "saves.py": ("def update(save_state):\n    save_state.last_sync = 1\n"),
            },
        )
        assert findings == []

    def test_multiple_violations_each_get_own_line(self, patched_check):
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "library.py": (
                    "def update(rom):\n    rom.cover_path = '/x'\n    rom.name = 'a'\n    rom.fs_name = 'b'\n"
                ),
            },
        )
        assert len(findings) == 3
        assert any("cover_path" in f for f in findings)
        assert any("name" in f for f in findings)
        assert any("fs_name" in f for f in findings)


class TestFindViolationsSkipPatterns:
    def test_self_receiver_skipped(self, patched_check):
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "service.py": (
                    "class Service:\n    def boot(self):\n        self._rom_id = 1\n        self._rom = None\n"
                ),
            },
        )
        assert findings == []

    def test_subscript_receiver_skipped(self, patched_check):
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "service.py": ("def update(roms):\n    roms['key'].cover_path = '/x'\n"),
            },
        )
        assert findings == []

    def test_nested_attribute_receiver_skipped(self, patched_check):
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "service.py": ("def update(repo):\n    repo.rom.cover_path = '/x'\n"),
            },
        )
        # repo.rom.cover_path = ... — outer receiver is repo.rom (Attribute), not Name. Skipped.
        assert findings == []


class TestFindViolationsHeuristicEdges:
    def test_substring_variable_not_flagged_only_exact_snake_match(self, patched_check):
        # Variables named ``romm_api`` and ``rom_state`` merely *contain* the
        # snake form of class ``Rom`` — the exact-identifier matcher does NOT
        # flag them. Only a variable named exactly ``rom`` matches.
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "fetcher.py": (
                    "def setup(romm_api, rom_state):\n    romm_api.timeout = 30\n    rom_state.files = {}\n"
                ),
            },
        )
        assert findings == []

    def test_exact_snake_match_flagged(self, patched_check):
        # The variable named exactly ``rom_install`` matches aggregate
        # ``RomInstall`` (snake_case of the CamelCase class name).
        _, findings = patched_check(
            domain_files={
                "rom_install.py": (
                    "from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass RomInstall:\n    pass\n"
                ),
            },
            services_files={
                "download.py": ("def update(rom_install):\n    rom_install.file_path = '/x'\n"),
            },
        )
        assert len(findings) == 1
        assert "rom_install.file_path" in findings[0]
        assert "RomInstall is @cosmic_aggregate" in findings[0]

    def test_escape_hatch_suppresses_finding(self, patched_check):
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "library.py": ("def update(rom):\n    rom.cover_path = '/x'  # pragma: no aggregate-check\n"),
            },
        )
        assert findings == []

    def test_aug_assign_also_flagged(self, patched_check):
        _, findings = patched_check(
            domain_files={
                "playtime.py": (
                    "from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Playtime:\n    pass\n"
                ),
            },
            services_files={
                "service.py": ("def add(playtime):\n    playtime.total_seconds += 60\n"),
            },
        )
        assert len(findings) == 1
        assert "playtime.total_seconds" in findings[0]

    def test_bare_ann_assign_without_value_not_flagged(self, patched_check):
        # ``rom.field: int`` is a type annotation, not a mutation. AnnAssign
        # nodes without a ``value`` must be skipped.
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "service.py": ("def annotate(rom):\n    rom.cover_path: str\n"),
            },
        )
        assert findings == []

    def test_ann_assign_with_value_still_flagged(self, patched_check):
        # ``rom.field: int = 1`` IS a mutation — locks in the other half of
        # the AnnAssign contract.
        _, findings = patched_check(
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "service.py": ("def assign(rom):\n    rom.cover_path: str = '/x'\n"),
            },
        )
        assert len(findings) == 1
        assert "rom.cover_path" in findings[0]

    def test_no_aggregates_no_findings(self, patched_check):
        # When no @cosmic_aggregate decorators exist anywhere, the check is a no-op.
        # Mirrors the PR 1 state of the codebase.
        names, findings = patched_check(
            services_files={
                "service.py": ("def update(rom):\n    rom.cover_path = '/x'\n"),
            },
        )
        assert names == set()
        assert findings == []


class TestToSnake:
    def test_single_word_lowercased(self):
        assert check._to_snake("Rom") == "rom"

    def test_camel_case_split_with_underscore(self):
        assert check._to_snake("RomInstall") == "rom_install"

    def test_multi_word_camel_case(self):
        assert check._to_snake("FirmwareCacheEntry") == "firmware_cache_entry"


class TestMainEntryPoint:
    def test_help_flag_returns_zero(self, capsys):
        rc = check.main(["--help"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Aggregate field-assignment ban" in captured.out

    def test_short_help_flag_returns_zero(self):
        rc = check.main(["-h"])
        assert rc == 0

    def test_real_repo_run_is_clean(self, capsys):
        # No @cosmic_aggregate-decorated classes exist in the real repo yet
        # (PR 1 state). The check must exit 0.
        rc = check.main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "OK:" in captured.out

    def test_main_reports_and_returns_one_on_violations(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys):
        domain_dir, services_dir = _make_fake_tree(
            tmp_path,
            domain_files={
                "rom.py": ("from domain._aggregate import cosmic_aggregate\n@cosmic_aggregate\nclass Rom:\n    pass\n"),
            },
            services_files={
                "library.py": ("def update(rom):\n    rom.cover_path = '/x'\n"),
            },
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "DOMAIN_DIR", domain_dir)
        monkeypatch.setattr(check, "SERVICES_DIR", services_dir)

        rc = check.main([])
        assert rc == 1
        captured = capsys.readouterr()
        assert "rom.cover_path" in captured.out
        assert "ERROR:" in captured.out
