"""Tests for ``scripts/check_read_only_module.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Fixtures write a single
module under ``tmp_path`` and point the check's declaration table at it, so the
scan runs exactly as it does in CI.

Coverage centres on the rule (a declared module calls repository reads only),
the read/write split re-derived from ``services/protocols/repositories.py`` and
pinned method by method, the receiver-shaped narrowing that keeps unrelated
two-deep calls out, and the documented blind spots — including the asymmetric
one: a write NAMED like a read passes in silence, which the gate's own texts
have to say out loud. A gate whose advertised reach outruns its real one is
worse than none, because the register quotes the advert.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_read_only_module.py"
_PROTOCOLS_PATH = _REPO_ROOT / "py_modules" / "services" / "protocols" / "repositories.py"

_MODULE = "py_modules/services/library/local_library_reader.py"
_REASON = "it only ever reads"

# The classification the gate must produce for every method the repository
# Protocols declare, pinned here so a new one fails until someone decides which
# half it belongs to. Reads first — these are what a read-only module may call.
_EXPECTED_READS = frozenset(
    {
        "count",
        "get",
        "get_all_emulator_overrides",
        "get_by_app_id",
        "get_cache_epoch",
        "get_latest_completed",
        "get_latest_terminal",
        "get_running",
        "iter_all",
        "iter_by_group_key",
        "iter_by_platform",
        "iter_page",
        "iter_pending_sessions",
        "rom_ids_with_pending_device",
    }
)
_EXPECTED_WRITES = frozenset(
    {
        "clear",
        "clear_all_applied_launch_options",
        "delete",
        "replace_all",
        "save",
        "set",
        "set_applied_launch_options",
        "set_emulator_override",
        "set_fs_size_bytes",
        "set_selected_disc",
    }
)


def _repository_protocol_methods() -> set[str]:
    """Every method name declared on a Protocol in ``services/protocols/repositories.py``."""
    tree = ast.parse(_PROTOCOLS_PATH.read_text(encoding="utf-8"))
    return {
        node.name
        for cls in tree.body
        if isinstance(cls, ast.ClassDef)
        for node in cls.body
        if isinstance(node, ast.FunctionDef)
    }


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

    def test_an_unparsable_declared_module_is_a_finding(self, scan):
        # A declared module the parser chokes on is a module nobody is checking:
        # the one path where "nothing to scan" must not read as "nothing wrong".
        findings = scan("def (:\n")
        assert len(findings) == 1
        assert "does not parse" in findings[0]


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

    def test_a_write_passed_as_a_bound_method_escapes(self, scan):
        # ``uow.roms.save`` as an argument is an Attribute, never a Call — and
        # this is the idiom every method in the declared module is itself
        # invoked through, so it is the shape a reader here reaches for.
        findings = scan("def go(loop, uow, rom):\n    return loop.run_in_executor(None, uow.roms.save, rom)\n")
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
        source = (_REPO_ROOT / "py_modules/services/protocols/uow.py").read_text(encoding="utf-8")
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

    def test_every_repository_protocol_method_is_classified_as_pinned(self):
        # The read/write split is what the gate IS, so it is pinned against the
        # protocol file rather than against a hand-kept list: a new repository
        # method lands in neither set and fails here, forcing the decision to be
        # made where it is reviewed. A hardcoded list would let a read-shaped
        # write (``get_or_create``) widen the gate in silence.
        methods = _repository_protocol_methods()
        assert methods == _EXPECTED_READS | _EXPECTED_WRITES, (
            "a repository Protocol method is unclassified — add it to _EXPECTED_READS or "
            "_EXPECTED_WRITES here, and check whether check_read_only_module.py agrees"
        )
        assert {name for name in methods if check._is_read(name)} == _EXPECTED_READS

    def test_the_pinned_partition_covers_both_directions(self):
        # Guards the assertion above against a vacuous pass: neither half may be
        # empty, and no name may sit in both.
        assert _EXPECTED_READS
        assert _EXPECTED_WRITES
        assert not (_EXPECTED_READS & _EXPECTED_WRITES)


class TestReadShapedWriteBlindSpot:
    """The asymmetry the docstring and the register entry both state.

    A read named outside the shapes fails loud (safe); a WRITE named like a read
    passes in silence. These two pin the direction so the texts cannot claim the
    classification errs safe both ways.
    """

    @pytest.mark.parametrize("name", ["get_or_create", "iter_and_purge", "get_and_delete"])
    def test_a_read_shaped_write_name_is_not_flagged(self, scan, name):
        assert scan(f"def go(uow):\n    uow.roms.{name}(1)\n") == []

    def test_a_read_named_outside_the_shapes_is_flagged(self, scan):
        # The safe direction: a genuine read the shapes do not cover fails until
        # someone adds it to READ_METHODS.
        findings = scan("def go(uow):\n    uow.roms.rom_ids_on_platform('n64')\n")
        assert len(findings) == 1


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
