"""Self-conformance of the full sync decision against the published gavel contract.

This tier proves the full per-``(rom, filename, slot)`` decision still conforms to the gavel
decision-table contract — the decision the spec models, extracted from this very codebase. The
vendored JSON vectors under ``tests/adapters/gavel_vectors/decision-table/`` are the contract's
normative artifact: each one pins the five positional inputs (``local_file``,
``server_saves_in_slot``, ``files_state``, ``device_id``, ``local_hash``) to the tagged decision
the spec mandates, so any drift between core and contract surfaces here as a failing vector.

Every vector runs against :class:`adapters.gavel_native.GavelNativeAdapter`, the compiled core
services actually decide through. It answers in the ``Skip`` / ``Upload`` / ``Download`` /
``Conflict`` dataclasses, which this test serializes into the vector dialect for comparison.
Vectors change only by a deliberate re-copy from upstream (see
``tests/adapters/gavel_vectors/README.md``) — never by editing them to match the core.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from adapters.gavel_native import GavelNativeAdapter
from domain.sync_action import Download, Skip, SyncAction, Upload

if TYPE_CHECKING:
    from services.protocols import ComputeSyncActionFn

_VECTORS_DIR = Path(__file__).parent / "gavel_vectors" / "decision-table"


@pytest.fixture(scope="module")
def core() -> ComputeSyncActionFn:
    """The compiled core, bound the way production binds it."""
    return GavelNativeAdapter().compute_sync_action


def _serialize(result: SyncAction) -> dict[str, Any]:
    """Map a ``SyncAction`` onto the decision-table vector dialect.

    The dialect is a tagged dict keyed by ``action``: ``skip`` carries ``reason`` +
    ``adopt_baseline``; ``upload`` carries ``target_save_id``; ``download`` / ``conflict``
    carry the chosen server save's ``id`` as ``server_save_id``.
    """
    if isinstance(result, Skip):
        return {"action": "skip", "reason": result.reason, "adopt_baseline": result.adopt_baseline}
    if isinstance(result, Upload):
        return {"action": "upload", "target_save_id": result.target_save_id}
    if isinstance(result, Download):
        return {"action": "download", "server_save_id": result.server_save["id"]}
    # The union is exhausted: ``result`` narrows to ``Conflict`` here, so a new
    # ``SyncAction`` variant would fail type-checking on the ``server_save`` access.
    return {"action": "conflict", "server_save_id": result.server_save["id"]}


def _load_vectors() -> tuple[list[tuple[dict[str, Any], dict[str, Any], str | None]], list[str]]:
    """Flatten every ``decision-table/*.json`` vector file into parametrize argvalues + ids.

    Returns a ``(argvalues, ids)`` pair. Each argvalue is ``(input, expected, rationale)`` — every
    decision-table vector carries a ``rationale``. Each id is ``<filestem>:<vector name>`` so a
    failure names the offending vector directly.
    """
    argvalues: list[tuple[dict[str, Any], dict[str, Any], str | None]] = []
    ids: list[str] = []
    for path in sorted(_VECTORS_DIR.glob("*.json")):
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        for vector in data["vectors"]:
            argvalues.append((vector["input"], vector["expected"], vector.get("rationale")))
            ids.append(f"{path.stem}:{vector['name']}")
    return argvalues, ids


_ARGVALUES, _IDS = _load_vectors()

# An empty glob is a vendoring regression (files deleted or moved), not a valid
# "nothing to test" state — fail loudly at collection rather than silently pass.
assert _ARGVALUES, f"no gavel decision-table vectors loaded from {_VECTORS_DIR}"


@pytest.mark.parametrize(("vector_input", "expected", "rationale"), _ARGVALUES, ids=_IDS)
def test_compute_sync_action_conforms(
    core: ComputeSyncActionFn,
    vector_input: dict[str, Any],
    expected: dict[str, Any],
    rationale: str | None,
) -> None:
    result = core(
        vector_input["local_file"],
        vector_input["server_saves_in_slot"],
        vector_input["files_state"],
        vector_input["device_id"],
        vector_input["local_hash"],
    )
    actual = _serialize(result)
    context = f" — {rationale}" if rationale else ""
    assert actual == expected, (
        f"gavel vector expected {expected!r}, the native core returned {actual!r} for input {vector_input!r}{context}"
    )
