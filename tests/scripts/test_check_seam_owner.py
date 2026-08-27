"""Tests for ``scripts/check_seam_owner.py``.

The check is loaded via ``importlib`` because ``scripts/`` is not on
``sys.path`` (and is excluded from ruff/basedpyright). Fixtures lay out a small
``py_modules/services/library/`` tree under ``tmp_path`` so the check walks it
with its real file discovery.

Coverage centres on the confinement rule (a seam is held only by the module its
``SEAM_OWNERS`` entry names), the two shapes of "holding" the scan sees (an
attribute named after the seam, an annotation naming its Protocol), the
per-seam nature of ownership (owning one seam grants nothing about another),
the composition root's narrow licence to **pass** a seam without **using** one,
and the documented blind spots.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "check_seam_owner.py"


def _load_check_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_seam_owner", _SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


check = _load_check_module()

# The owner table the fixtures run against — the real one, restated so a
# deliberate change to production ownership shows up as a test edit.
_OWNERS = {
    "active_core": frozenset({"bake_inputs.py"}),
    "disc_resolver": frozenset({"bake_inputs.py"}),
    "renderer_rss": frozenset({"session_budget.py"}),
    "renderer_gc": frozenset({"session_budget.py"}),
}


def _make_library_tree(tmp_path: Path, files: dict[str, str]) -> Path:
    """Build a fake ``py_modules/services/library/`` tree: ``name -> source``."""
    library_dir = tmp_path / "py_modules" / "services" / "library"
    library_dir.mkdir(parents=True)
    for name, source in files.items():
        (library_dir / name).write_text(source, encoding="utf-8")
    return library_dir


@pytest.fixture
def patched_check(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Yield a helper that retargets the check at a tmp-path library tree + runs it."""

    def _run(files: dict[str, str]) -> list[str]:
        library_dir = _make_library_tree(tmp_path, files)
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "LIBRARY_DIR", library_dir)
        monkeypatch.setattr(check, "SEAM_OWNERS", dict(_OWNERS))
        return check.find_violations(check._iter_scanned_files(library_dir))

    return _run


class TestFindViolations:
    def test_flags_seam_read_in_a_foreign_module(self, patched_check):
        holder = "class H:\n    def __init__(self, config):\n        self._a = config.active_core\n"
        findings = patched_check({"bake_inputs.py": holder, "sync_orchestrator.py": holder})
        assert len(findings) == 1
        assert "sync_orchestrator.py" in findings[0]
        assert "active_core" in findings[0]
        assert "bake_inputs.py" in findings[0]  # the message names the owner

    def test_flags_protocol_annotation_in_a_foreign_module(self, patched_check):
        findings = patched_check(
            {
                "sync_orchestrator.py": "class Config:\n    disc_resolver: DiscResolver\n",
            }
        )
        assert len(findings) == 1
        assert "DiscResolver" in findings[0]
        assert "disc_resolver" in findings[0]

    def test_flags_annotation_nested_in_a_subscript(self, patched_check):
        # ``RendererRssFn | None`` / ``list[RendererRssFn]`` reach the Protocol
        # name through a BinOp / Subscript, which the walk descends into.
        findings = patched_check(
            {
                "reporter.py": "def go(rss: RendererRssFn | None) -> None:\n    return None\n",
            }
        )
        assert len(findings) == 1
        assert "renderer_rss" in findings[0]

    def test_flags_annotated_return(self, patched_check):
        findings = patched_check(
            {
                "fetcher.py": "def get() -> RendererGcFn:\n    raise NotImplementedError\n",
            }
        )
        assert len(findings) == 1
        assert "renderer_gc" in findings[0]

    def test_ownership_is_per_seam_not_per_module(self, patched_check):
        # session_budget.py owns the renderer seams and nothing else — reaching
        # for disc_resolver there is exactly the drift this gate exists for.
        findings = patched_check(
            {
                "session_budget.py": (
                    "class M:\n    def __init__(self, config):\n"
                    "        self._gc = config.renderer_gc\n"
                    "        self._d = config.disc_resolver\n"
                ),
            }
        )
        assert len(findings) == 1
        assert "disc_resolver" in findings[0]

    def test_owner_module_not_flagged(self, patched_check):
        findings = patched_check(
            {
                "bake_inputs.py": (
                    "class Config:\n    active_core: ActiveCoreReader\n    disc_resolver: DiscResolver\n\n"
                    "class B:\n    def __init__(self, config):\n"
                    "        self._a = config.active_core\n        self._d = config.disc_resolver\n"
                ),
            }
        )
        assert findings == []

    def test_unrelated_attribute_and_annotation_not_flagged(self, patched_check):
        findings = patched_check(
            {
                "reporter.py": (
                    "class Config:\n    artwork: ArtworkManager\n\n"
                    "def go(config):\n    return config.artwork, config.active_core_label\n"
                ),
            }
        )
        assert findings == []

    def test_syntax_error_file_is_skipped(self, patched_check):
        findings = patched_check(
            {
                "broken.py": "def (:\n",
                "reporter.py": "def go(config):\n    return config.renderer_rss\n",
            }
        )
        assert len(findings) == 1
        assert "reporter.py" in findings[0]

    def test_every_seam_is_reported_once_per_site(self, patched_check):
        findings = patched_check(
            {
                "reporter.py": (
                    "def go(config):\n"
                    "    return config.active_core, config.disc_resolver, config.renderer_rss, config.renderer_gc\n"
                ),
            }
        )
        assert len(findings) == 4


class TestCompositionRoot:
    """A composition root may PASS a seam on; it may not USE one."""

    def test_facade_wiring_is_not_flagged(self, patched_check):
        # The seam arrives annotated on LibraryServiceConfig and leaves as a
        # keyword-argument value — the whole of the real façade's seam contact.
        findings = patched_check(
            {
                "service.py": (
                    "class LibraryServiceConfig:\n"
                    "    active_core: ActiveCoreReader\n"
                    "    disc_resolver: DiscResolver\n"
                    "    renderer_rss: RendererRssFn\n"
                    "    renderer_gc: RendererGcFn\n\n"
                    "class LibraryService:\n"
                    "    def __init__(self, config):\n"
                    "        self._bake = ShortcutBakeInputs(\n"
                    "            config=ShortcutBakeInputsConfig(\n"
                    "                active_core=config.active_core, disc_resolver=config.disc_resolver\n"
                    "            )\n"
                    "        )\n"
                    "        self._budget = SessionBudgetMonitor(\n"
                    "            config=SessionBudgetMonitorConfig(\n"
                    "                renderer_rss=config.renderer_rss, renderer_gc=config.renderer_gc\n"
                    "            )\n"
                    "        )\n"
                ),
            }
        )
        assert findings == []

    def test_facade_method_that_uses_a_seam_is_flagged(self, patched_check):
        # The regression a blanket file exemption could not tell from wiring:
        # the façade grows a second holder of a UoW-opening seam.
        findings = patched_check(
            {
                "service.py": (
                    "class LibraryServiceConfig:\n    active_core: ActiveCoreReader\n\n"
                    "class LibraryService:\n"
                    "    def peek(self, rom_id):\n"
                    "        return self._config.active_core.active_emulator_for_rom(rom_id)\n"
                ),
            }
        )
        assert len(findings) == 1
        assert "service.py" in findings[0]
        assert "active_core" in findings[0]

    def test_facade_storing_a_seam_on_itself_is_flagged(self, patched_check):
        # Storing is holding, not passing: the assignment's RHS is not a
        # keyword-argument value.
        findings = patched_check(
            {
                "service.py": (
                    "class LibraryServiceConfig:\n    disc_resolver: DiscResolver\n\n"
                    "class LibraryService:\n"
                    "    def __init__(self, config):\n        self._disc = config.disc_resolver\n"
                ),
            }
        )
        assert len(findings) == 1
        assert "disc_resolver" in findings[0]

    def test_seam_annotation_on_a_non_facade_config_is_flagged(self, patched_check):
        # Only LibraryServiceConfig is bootstrap's delivery point; a second
        # config declaring the same seam is a second holder.
        findings = patched_check(
            {
                "sync_orchestrator.py": "class SyncOrchestratorConfig:\n    active_core: ActiveCoreReader\n",
            }
        )
        assert len(findings) == 1
        assert "active_core" in findings[0]

    def test_facade_config_field_of_another_class_in_the_same_file_is_flagged(self, patched_check):
        findings = patched_check(
            {
                "service.py": (
                    "class LibraryServiceConfig:\n    renderer_rss: RendererRssFn\n\n"
                    "class OtherConfig:\n    renderer_rss: RendererRssFn\n"
                ),
            }
        )
        assert len(findings) == 1
        assert "renderer_rss" in findings[0]

    def test_pass_through_licence_does_not_leak_to_the_receiver(self, patched_check):
        # A keyword value is exempt; the same attribute read into a local or
        # returned is not.
        findings = patched_check(
            {
                "reporter.py": (
                    "def wire(config):\n    Thing(active_core=config.active_core)\n    return config.active_core\n"
                ),
            }
        )
        assert len(findings) == 1
        assert findings[0].endswith("the 'active_core' seam is held only by bake_inputs.py.")


class TestDocumentedBlindSpots:
    def test_private_holding_attribute_alone_is_not_flagged(self, patched_check):
        # Only the seam's own name is matched, so a module that already holds it
        # under ``_active_core`` is invisible — the gate catches the injection,
        # not every later use.
        findings = patched_check(
            {
                "sync_orchestrator.py": "def go(self):\n    return self._active_core.active_emulator_for_rom(1)\n",
            }
        )
        assert findings == []

    def test_string_annotation_is_not_flagged(self, patched_check):
        # A quoted annotation is a Constant, not a Name.
        findings = patched_check(
            {
                "sync_orchestrator.py": 'class Config:\n    resolver_alias: "DiscResolver"\n',
            }
        )
        assert findings == []

    def test_aliased_protocol_import_escapes_the_annotation_half(self, patched_check):
        # ``ActiveCoreReader as CoreReader`` leaves the annotation leaf
        # unmatched, and the field NAME is never inspected — but the ctor read
        # that unpacks the config is still flagged, which is why this is
        # precision in the reach rather than a way through.
        annotation_only = (
            "from services.protocols import ActiveCoreReader as CoreReader\n\n"
            "class Config:\n    active_core: CoreReader\n"
        )
        assert patched_check({"sync_orchestrator.py": annotation_only}) == []

    def test_the_constructor_read_still_catches_the_aliased_case(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
        library_dir = _make_library_tree(
            tmp_path,
            {
                "sync_orchestrator.py": (
                    "from services.protocols import ActiveCoreReader as CoreReader\n\n"
                    "class Config:\n    active_core: CoreReader\n\n"
                    "class O:\n    def __init__(self, config):\n        self._a = config.active_core\n"
                ),
            },
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "LIBRARY_DIR", library_dir)
        monkeypatch.setattr(check, "SEAM_OWNERS", dict(_OWNERS))
        findings = check.find_violations(check._iter_scanned_files(library_dir))
        assert len(findings) == 1
        assert "active_core" in findings[0]


class TestMainEntryPoint:
    def test_help_flag_returns_zero(self, capsys):
        rc = check.main(["--help"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Seam-owner confinement gate" in captured.out

    def test_real_repo_run_is_clean(self, capsys):
        # The real library package holds each seam only in its owner module,
        # and the façade only passes them on — the check must exit 0.
        rc = check.main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "OK:" in captured.out

    def test_main_reports_and_returns_one_on_planted_violation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
    ):
        library_dir = _make_library_tree(
            tmp_path,
            {
                "sync_orchestrator.py": "def go(config):\n    return config.disc_resolver\n",
            },
        )
        monkeypatch.setattr(check, "REPO_ROOT", tmp_path)
        monkeypatch.setattr(check, "LIBRARY_DIR", library_dir)

        rc = check.main([])
        assert rc == 1
        captured = capsys.readouterr()
        assert "disc_resolver" in captured.out
        assert "ERROR:" in captured.out


class TestSeamTable:
    def test_every_protocol_maps_to_a_known_seam(self):
        assert set(check.SEAM_PROTOCOLS.values()) <= set(check.SEAM_OWNERS)

    def test_every_seam_has_a_protocol(self):
        # A seam with no Protocol entry would be enforced on the attribute name
        # only, silently halving the scan.
        assert set(check.SEAM_PROTOCOLS.values()) == set(check.SEAM_OWNERS)

    def test_no_owner_set_names_the_facade(self):
        # The façade's licence is structural (pass-through only). Re-adding it
        # as an owner would restore the hole this narrowing closed.
        assert all("service.py" not in owners for owners in check.SEAM_OWNERS.values())
