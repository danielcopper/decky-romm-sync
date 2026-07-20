"""Tests for the GavelNativeAdapter — the compiled 409-resolution kernel.

Three tiers guard the shipped shared object:

* **Unit** — the real vendored ``.so`` loads, decides the canonical cases
  (including the ``None`` vs ``""`` distinction), and a bad path raises the
  distinct :class:`GavelNativeLoadError` naming the path.
* **Vendored-vector conformance** — every ``ladder`` gavel vector is replayed
  against the adapter, so the shipped binary must satisfy the same normative
  contract the in-tree kernel is held to (``tests/domain/test_sync_action_gavel_vectors.py``).
* **Differential** — the adapter and ``domain.sync_action.resolve_upload_conflict``
  agree on every point of the ``6^4`` hash alphabet (``None``, ``""``, four
  distinct hashes), so the native core and the Python oracle are indistinguishable.
"""

from __future__ import annotations

import ctypes.util
import itertools
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from adapters.gavel_native import GavelNativeAdapter, GavelNativeLoadError
from domain.sync_action import resolve_upload_conflict

if TYPE_CHECKING:
    from collections.abc import Iterator

# Distinct-hash alphabet for the differential: two "unknown" spellings the
# kernel must treat identically (None / "") plus four distinct real hashes.
_ALPHABET: tuple[str | None, ...] = (None, "", "h1", "h2", "h3", "h4")

_LADDER_VECTORS_DIR = Path(__file__).parent.parent / "domain" / "gavel_vectors" / "ladder"


@pytest.fixture(scope="module")
def adapter() -> Iterator[GavelNativeAdapter]:
    """The real adapter over the vendored ``py_modules/native/libgavel-x86_64-linux.so``."""
    yield GavelNativeAdapter()


class TestLoad:
    def test_loads_vendored_shared_object(self, adapter: GavelNativeAdapter) -> None:
        # A successful construction proves the vendored .so opened and the
        # symbol bound; a canonical decision proves the call path works.
        assert adapter("h1", "h1", None, None) == "download"

    def test_bad_path_raises_distinct_error_naming_the_path(self) -> None:
        bad_path = "/nonexistent/does-not-exist/libgavel.so"
        with pytest.raises(GavelNativeLoadError, match=bad_path):
            GavelNativeAdapter(lib_path=bad_path)

    def test_loadable_library_missing_symbol_raises_distinct_error(self) -> None:
        # A real, loadable shared object that lacks the gavel symbol must fail
        # the same distinct way as a missing file — never a raw AttributeError.
        libc = ctypes.util.find_library("c")
        if libc is None:
            pytest.skip("libc not locatable on this platform")
        with pytest.raises(GavelNativeLoadError, match="missing symbol"):
            GavelNativeAdapter(lib_path=libc)


class TestDecisions:
    def test_unchanged_since_baseline_downloads(self, adapter: GavelNativeAdapter) -> None:
        # local == last_sync (both truthy) → no un-synced work → adopt server.
        assert adapter("same", "same", "server", "other") == "download"

    def test_parity_match_downloads(self, adapter: GavelNativeAdapter) -> None:
        # Diverged from baseline but byte-identical to the server head → download.
        assert adapter("srv", "base", "srv", None) == "download"

    def test_two_sided_divergence_is_conflict(self, adapter: GavelNativeAdapter) -> None:
        assert adapter("local", "base", "server", "base") == "conflict"

    def test_all_unknown_is_conflict(self, adapter: GavelNativeAdapter) -> None:
        assert adapter(None, None, None, None) == "conflict"

    def test_empty_string_is_not_a_match(self, adapter: GavelNativeAdapter) -> None:
        # "" reads as unknown, never as "provably unchanged" — the safe default
        # under uncertainty is conflict. This pins the None-vs-"" boundary the
        # ctypes marshalling must preserve.
        assert adapter("", "", None, None) == "conflict"
        assert adapter("", "", "", "") == "conflict"

    def test_empty_local_with_real_baseline_is_conflict(self, adapter: GavelNativeAdapter) -> None:
        # An empty local hash can't equal a real baseline "provably".
        assert adapter("", "base", "base", None) == "conflict"


def _load_ladder_vectors() -> tuple[list[tuple[dict[str, str | None], str]], list[str]]:
    """Flatten every ``gavel_vectors/ladder/*.json`` file into (argvalue, id) pairs."""
    argvalues: list[tuple[dict[str, str | None], str]] = []
    ids: list[str] = []
    for path in sorted(_LADDER_VECTORS_DIR.glob("*.json")):
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        for vector in data["vectors"]:
            argvalues.append((vector["input"], vector["expected"]))
            ids.append(f"{path.stem}:{vector['name']}")
    return argvalues, ids


_LADDER_ARGVALUES, _LADDER_IDS = _load_ladder_vectors()

# An empty glob is a vendoring regression (files deleted or moved), not a valid
# "nothing to test" state — fail loudly at collection rather than silently pass.
assert _LADDER_ARGVALUES, f"no gavel ladder vectors loaded from {_LADDER_VECTORS_DIR}"


class TestLadderConformance:
    @pytest.mark.parametrize(("vector_input", "expected"), _LADDER_ARGVALUES, ids=_LADDER_IDS)
    def test_adapter_conforms_to_ladder_vector(
        self,
        adapter: GavelNativeAdapter,
        vector_input: dict[str, str | None],
        expected: str,
    ) -> None:
        result = adapter(
            vector_input["local_hash"],
            vector_input["last_sync_hash"],
            vector_input["server_content_hash"],
            vector_input["last_sync_server_hash"],
        )
        assert result == expected, (
            f"gavel ladder vector expected {expected!r}, native core returned {result!r} for input {vector_input!r}"
        )


class TestDifferential:
    def test_adapter_matches_python_kernel_over_full_alphabet(self, adapter: GavelNativeAdapter) -> None:
        """The native core and the Python oracle agree on every 6^4 point."""
        mismatches: list[str] = []
        for local, last_sync, server, last_sync_server in itertools.product(_ALPHABET, repeat=4):
            native = adapter(local, last_sync, server, last_sync_server)
            oracle = resolve_upload_conflict(local, last_sync, server, last_sync_server)
            if native != oracle:
                mismatches.append(
                    f"({local!r}, {last_sync!r}, {server!r}, {last_sync_server!r}): native={native!r} oracle={oracle!r}"
                )
        assert not mismatches, "native core diverged from the Python kernel:\n" + "\n".join(mismatches)
