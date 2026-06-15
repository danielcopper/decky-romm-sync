"""Backend↔frontend emit-event parity, surfaced inside the pytest run.

This pins the exact contract the CI gate (``scripts/check_event_parity.py``)
enforces: every backend ``emit("name", ...)`` event has a matching frontend
``addEventListener("name", ...)`` listener, in both directions. It is the
static-parity sibling of the rest of ``tests/contract/`` — those tests *drive*
the real callables frontend-shaped; this one asserts the two *declarations* of
the event channel agree, so a renamed/added/removed event or an orphaned
listener breaks the pytest run, not just the standalone lint gate.

The parser functions are imported from the gate script (loaded via ``importlib``
because ``scripts/`` is not on ``sys.path``), so the test and the CI gate share
one implementation — they can never disagree about what "matches" means.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_event_parity.py"
_SRC_DIR = _REPO_ROOT / "src"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_event_parity", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_gate = _load_gate()


def test_backend_frontend_event_parity_matches():
    backend = _gate.parse_backend_emits()
    frontend = _gate.parse_frontend_listeners(_SRC_DIR)
    discrepancies = _gate.find_discrepancies(backend, frontend, _gate.EXEMPT)
    assert discrepancies == []
