"""Self-conformance of the 409 resolution ladder against the published gavel contract.

This tier proves ``domain.sync_action.resolve_upload_conflict`` still conforms to the gavel
ladder contract — the normative 409-resolution spec that was extracted from this very kernel.
The vendored JSON vectors under ``gavel_vectors/`` are the contract's normative artifact: each
one pins a ``(local_hash, last_sync_hash, server_content_hash, last_sync_server_hash)`` input
to the ladder outcome the spec mandates, so any drift between kernel and contract surfaces here
as a failing vector. Vectors change only by a deliberate re-copy from upstream (see
``gavel_vectors/README.md``) — never by editing them to match the kernel.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from domain.sync_action import resolve_upload_conflict

_VECTORS_DIR = Path(__file__).parent / "gavel_vectors"


def _load_vectors() -> tuple[list[tuple[dict[str, str | None], str, str | None]], list[str]]:
    """Flatten every ``gavel_vectors/*.json`` file into parametrize argvalues + ids.

    Returns a ``(argvalues, ids)`` pair. Each argvalue is
    ``(input, expected, rationale)`` — ``rationale`` is ``None`` for the exhaustive
    equivalence-class vectors, present on the curated named cases. Each id is
    ``<filestem>:<vector name>`` so a failure names the offending vector directly.
    """
    argvalues: list[tuple[dict[str, str | None], str, str | None]] = []
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
assert _ARGVALUES, f"no gavel vectors loaded from {_VECTORS_DIR}"


@pytest.mark.parametrize(("vector_input", "expected", "rationale"), _ARGVALUES, ids=_IDS)
def test_resolve_upload_conflict_conforms(
    vector_input: dict[str, str | None],
    expected: str,
    rationale: str | None,
) -> None:
    result = resolve_upload_conflict(
        vector_input["local_hash"],
        vector_input["last_sync_hash"],
        vector_input["server_content_hash"],
        vector_input["last_sync_server_hash"],
    )
    context = f" — {rationale}" if rationale else ""
    assert result == expected, (
        f"gavel vector expected {expected!r}, kernel returned {result!r} for input {vector_input!r}{context}"
    )
