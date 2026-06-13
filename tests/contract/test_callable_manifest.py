"""Frontend↔backend callable-manifest parity, surfaced inside the pytest run.

This pins the exact contract the CI gate (``scripts/check_callable_manifest.py``)
enforces: every ``callable<[Args], Return>("name")`` declared on the frontend
(``src/**/*.ts``) has a matching public ``async def name`` on the ``Plugin``
class in ``main.py``, in both directions, with matching arity. It is the
static-parity sibling of the rest of ``tests/contract/`` — those tests *drive*
the real callables frontend-shaped; this one asserts the two *declarations*
agree before any callable is driven, so a renamed/added/removed callable or an
arity drift breaks the pytest run, not just the standalone lint gate.

The parser functions are imported from the gate script (loaded via ``importlib``
because ``scripts/`` is not on ``sys.path``), so the test and the CI gate share
one implementation — they can never disagree about what "matches" means.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT_PATH = _REPO_ROOT / "scripts" / "check_callable_manifest.py"
_SRC_DIR = _REPO_ROOT / "src"
_MAIN_PY = _REPO_ROOT / "main.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("check_callable_manifest", _SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_gate = _load_gate()


def test_frontend_backend_callable_manifest_matches():
    frontend = _gate.parse_frontend_callables(_SRC_DIR)
    backend = _gate.parse_backend_callables(_MAIN_PY)
    discrepancies = _gate.find_discrepancies(frontend, backend, _gate.EXEMPT)
    assert discrepancies == []
