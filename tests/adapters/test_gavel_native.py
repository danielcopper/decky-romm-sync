"""Tests for the GavelNativeAdapter — the compiled save-sync decision core.

Two of the tiers guarding the shipped shared object live here, for both of the
decisions it owns (the upload-409 ladder and the full per-file sync action):

* **Unit** — the real vendored ``.so`` loads, decides the canonical cases
  (including the ``None`` vs ``""`` distinction and the marshalling boundaries
  where an "unknown" must stay a flag rather than become a value), and a bad
  path raises the distinct :class:`GavelNativeLoadError` naming the path.
* **Vendored-vector conformance** — every ``ladder`` gavel vector is replayed
  against the adapter, so the shipped binary must satisfy the normative contract
  the decision is held to. The decision-table family runs in
  ``tests/adapters/test_gavel_native_table_vectors.py``.
"""

from __future__ import annotations

import ctypes.util
import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from adapters.gavel_native import (
    GavelNativeAdapter,
    GavelNativeLoadError,
    _decode_action,
    _SyncActionResult,
)
from domain.sync_action import Conflict, Download, Skip, Upload

if TYPE_CHECKING:
    from collections.abc import Iterator

_LADDER_VECTORS_DIR = Path(__file__).parent / "gavel_vectors" / "ladder"


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


# ---------------------------------------------------------------------------
# The full sync decision — gavel_compute_sync_action
# ---------------------------------------------------------------------------

_DEVICE = "device-a"
_OTHER_DEVICE = "device-b"
_SERVER_HASH = "server-content-hash"
_BASELINE_HASH = "baseline-hash"
_HEAD_UPDATED_AT = "2026-06-02T12:00:00Z"
_OLDER_UPDATED_AT = "2026-06-01T12:00:00Z"
_HEAD_EPOCH = datetime.fromisoformat(_HEAD_UPDATED_AT.replace("Z", "+00:00")).timestamp()
_MTIME_NEWER = _HEAD_EPOCH + 3600


def _save(
    save_id: int,
    *,
    updated_at: str,
    content_hash: str | None = _SERVER_HASH,
    device_syncs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A RomM server-save dict in the shape ``list_saves`` returns it.

    ``device_syncs`` is omitted entirely when None, which is a distinct input
    from an empty list and is what an older RomM payload actually looks like.
    """
    save: dict[str, Any] = {"id": save_id, "updated_at": updated_at, "content_hash": content_hash}
    if device_syncs is not None:
        save["device_syncs"] = device_syncs
    return save


def _local(*, size: int | None, mtime: float | None) -> dict[str, Any]:
    """The local-file input shape ``MatrixExecutor._build_local_input`` produces."""
    return {"filename": "game.srm", "path": "/saves/game.srm", "size": size, "mtime": mtime}


def _ours(*, is_current: bool) -> list[dict[str, Any]]:
    return [{"device_id": _DEVICE, "is_current": is_current}]


_THEIRS = [{"device_id": _OTHER_DEVICE, "is_current": True}]


class TestDecisionTable:
    """The marshalling boundaries a wrong binding would cross silently."""

    def test_empty_slot_uploads_a_present_local_file(self, adapter: GavelNativeAdapter) -> None:
        action = adapter.compute_sync_action(_local(size=8192, mtime=_MTIME_NEWER), [], {}, _DEVICE, _BASELINE_HASH)
        assert action == Upload(target_save_id=None)

    def test_empty_slot_without_local_file_has_nothing_to_sync(self, adapter: GavelNativeAdapter) -> None:
        action = adapter.compute_sync_action(None, [], {}, _DEVICE, None)
        assert action == Skip(reason="nothing_to_sync", adopt_baseline=False)

    def test_unknown_action_raises_instead_of_decoding_as_conflict(self) -> None:
        # An action value this adapter does not know means the vendored .so and
        # this code disagree about the ABI. Conflict is a plausible-looking
        # decision to fall through to, which is exactly why it must not be the
        # fallback — the module's posture is to fail loudly, not substitute.
        result = _SyncActionResult(action=99)
        with pytest.raises(ValueError, match="unknown action 99"):
            _decode_action(result, {})

    def test_download_carries_the_callers_own_server_save_dict(self, adapter: GavelNativeAdapter) -> None:
        # The core answers with an id; the adapter has to resolve it back to the
        # very dict the caller passed, because five downstream sites read the
        # full save (updated_at, file_size_bytes, content_hash), not just its id.
        older = _save(100, updated_at=_OLDER_UPDATED_AT, device_syncs=_THEIRS)
        head = _save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_THEIRS)
        action = adapter.compute_sync_action(None, [older, head], {}, _DEVICE, None)
        assert isinstance(action, Download)
        assert action.server_save is head

    def test_conflict_carries_the_callers_own_server_save_dict(self, adapter: GavelNativeAdapter) -> None:
        head = _save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_ours(is_current=False))
        action = adapter.compute_sync_action(
            _local(size=8192, mtime=_MTIME_NEWER),
            [head],
            {"last_sync_hash": _BASELINE_HASH},
            _DEVICE,
            "drifted-hash",
        )
        assert isinstance(action, Conflict)
        assert action.server_save is head

    def test_unparseable_timestamp_never_wins_head_selection(self, adapter: GavelNativeAdapter) -> None:
        # The ISO string is parsed on this side of the boundary; "unparseable"
        # must arrive as a clear has_updated_at flag, never as a substitute
        # instant that could out-sort a real one.
        unparseable = _save(102, updated_at="not-a-timestamp", device_syncs=_THEIRS)
        parseable = _save(101, updated_at=_OLDER_UPDATED_AT, device_syncs=_THEIRS)
        action = adapter.compute_sync_action(None, [unparseable, parseable], {}, _DEVICE, None)
        assert isinstance(action, Download)
        assert action.server_save is parseable

    def test_unmeasurable_local_file_is_present_not_missing(self, adapter: GavelNativeAdapter) -> None:
        # Presence rides on the pointer alone. A file whose size and mtime are
        # both unknown still exists — reading the cleared has_* flags as "no
        # local file" would silently download over it.
        head = _save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_ours(is_current=False))
        action = adapter.compute_sync_action(_local(size=None, mtime=None), [head], {}, _DEVICE, "drifted-hash")
        assert action == Conflict(server_save=head)

    def test_zero_byte_local_is_a_size_not_an_absent_one(self, adapter: GavelNativeAdapter) -> None:
        # 0 is exactly what the corrupt-local guard looks for, so it must not be
        # marshalled as "no size recorded" — that would upload the truncated file.
        head = _save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_ours(is_current=True))
        action = adapter.compute_sync_action(
            _local(size=0, mtime=_MTIME_NEWER),
            [head],
            {"last_sync_hash": _BASELINE_HASH, "last_sync_local_size": 8192},
            _DEVICE,
            "drifted-hash",
        )
        assert action == Conflict(server_save=head)

    def test_adopt_baseline_survives_the_boundary(self, adapter: GavelNativeAdapter) -> None:
        head = _save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_ours(is_current=True))
        action = adapter.compute_sync_action(_local(size=8192, mtime=_MTIME_NEWER), [head], {}, _DEVICE, _SERVER_HASH)
        assert action == Skip(reason="synced", adopt_baseline=True)

    def test_head_without_a_device_syncs_key_decides_on_its_content_hash(self, adapter: GavelNativeAdapter) -> None:
        # A RomM payload can omit ``device_syncs`` entirely, so the head arrives
        # with no sync entries and its ``content_hash`` is the only evidence that
        # the local bytes are already on the server. Lost across the marshalling
        # boundary, the decision is made on unproven identity: a conflict prompt
        # for a file that is in sync.
        head = _save(101, updated_at=_HEAD_UPDATED_AT)
        action = adapter.compute_sync_action(_local(size=8192, mtime=_MTIME_NEWER), [head], {}, _DEVICE, _SERVER_HASH)
        assert action == Skip(reason="synced", adopt_baseline=True)

    def test_upload_echoes_the_superseded_save_id(self, adapter: GavelNativeAdapter) -> None:
        head = _save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_ours(is_current=True))
        action = adapter.compute_sync_action(
            _local(size=8192, mtime=_MTIME_NEWER),
            [head],
            {"last_sync_hash": _BASELINE_HASH},
            _DEVICE,
            "drifted-hash",
        )
        assert action == Upload(target_save_id=101)

    def test_non_integral_size_is_refused(self, adapter: GavelNativeAdapter) -> None:
        # An int64_t cannot carry it and rounding would land on 0 — the one
        # value the corrupt-local guard reacts to. Refuse rather than answer a
        # different question.
        local_file = _local(size=1.5, mtime=None)  # pyright: ignore[reportArgumentType]
        with pytest.raises(ValueError, match="whole number of bytes"):
            adapter.compute_sync_action(local_file, [], {}, _DEVICE, None)

    def test_empty_local_file_dict_on_an_empty_slot_is_a_present_file(self, adapter: GavelNativeAdapter) -> None:
        """``local_file={}`` is a file that exists and could not be measured.

        Presence rides on the pointer alone, never on the ``has_*`` flags: an
        empty dict is still a non-NULL pointer, so the empty slot has something
        to upload. Reading the cleared flags as "no local file" would answer
        ``nothing_to_sync`` and leave the only copy of that save unsynced.
        """
        assert adapter.compute_sync_action({}, [], {}, _DEVICE, None) == Upload(target_save_id=None)
