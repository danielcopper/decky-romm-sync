"""Tests for the GavelNativeAdapter — the compiled save-sync decision kernels.

Three tiers guard the shipped shared object, for both of the decisions it owns
(the upload-409 ladder and the full per-file sync action):

* **Unit** — the real vendored ``.so`` loads, decides the canonical cases
  (including the ``None`` vs ``""`` distinction and the marshalling boundaries
  where an "unknown" must stay a flag rather than become a value), and a bad
  path raises the distinct :class:`GavelNativeLoadError` naming the path.
* **Vendored-vector conformance** — every ``ladder`` gavel vector is replayed
  against the adapter, so the shipped binary must satisfy the same normative
  contract the in-tree kernel is held to (``tests/domain/test_sync_action_gavel_vectors.py``).
  The decision-table family runs against both kernels in
  ``tests/domain/test_sync_action_gavel_table_vectors.py``.
* **Differential** — the adapter and the in-tree ``domain.sync_action`` kernels
  agree on every point of a crossed input space: the ``6^4`` hash alphabet for
  the ladder, and local-file forms crossed with every ``device_syncs`` branch,
  both timestamp-parseability outcomes, and every baseline combination for the
  decision table.
"""

from __future__ import annotations

import ctypes.util
import itertools
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
from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    SyncAction,
    Upload,
    compute_sync_action,
    resolve_upload_conflict,
)

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
# One hour either side of the head, so the no-entry branch's timestamp
# fall-through is crossed in both directions.
_MTIME_NEWER = _HEAD_EPOCH + 3600
_MTIME_OLDER = _HEAD_EPOCH - 3600


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

# Every local-file form the executor can hand in, plus the two boundaries the
# marshalling has to keep apart: a file that exists but could not be measured
# (present — the pointer says so, not the has_* flags) and a 0-byte one (a real
# size the corrupt-local guard reacts to, not an absent one).
_LOCAL_FILES: dict[str, dict[str, Any] | None] = {
    "missing": None,
    "empty-dict": {},
    "unmeasurable": _local(size=None, mtime=None),
    "newer": _local(size=8192, mtime=_MTIME_NEWER),
    "older": _local(size=8192, mtime=_MTIME_OLDER),
    "zero-byte": _local(size=0, mtime=_MTIME_NEWER),
    "shrunk": _local(size=100, mtime=_MTIME_NEWER),
}

# All three device_syncs branches, both timestamp-parseability outcomes, a head
# with no content_hash, and a slot where the head is not the save this device is
# current on.
_SLOTS: dict[str, list[dict[str, Any]]] = {
    "empty": [],
    "current": [_save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_ours(is_current=True))],
    "not-current": [_save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_ours(is_current=False))],
    "no-entry": [_save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_THEIRS)],
    "no-device-syncs-key": [_save(101, updated_at=_HEAD_UPDATED_AT)],
    "head-without-content-hash": [
        _save(101, updated_at=_HEAD_UPDATED_AT, content_hash=None, device_syncs=_ours(is_current=False))
    ],
    "current-on-older-save": [
        _save(100, updated_at=_OLDER_UPDATED_AT, device_syncs=_ours(is_current=True)),
        _save(101, updated_at=_HEAD_UPDATED_AT, device_syncs=_THEIRS),
    ],
    "unparseable-timestamp-first": [
        _save(102, updated_at="not-a-timestamp", device_syncs=_THEIRS),
        _save(101, updated_at=_OLDER_UPDATED_AT, device_syncs=_ours(is_current=False)),
    ],
}

_LOCAL_HASHES: tuple[str | None, ...] = (None, "", _BASELINE_HASH, _SERVER_HASH, "drifted-hash")
_LAST_SYNC_HASHES: tuple[str | None, ...] = (None, "", _BASELINE_HASH)
_LAST_SYNC_SERVER_HASHES: tuple[str | None, ...] = (None, "", _SERVER_HASH)
_LAST_SYNC_LOCAL_SIZES: tuple[int | None, ...] = (None, 0, 200, 10000)


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

    def test_empty_local_file_dict_on_an_empty_slot_is_the_one_known_divergence(
        self, adapter: GavelNativeAdapter
    ) -> None:
        """``local_file={}`` is the single point where the two kernels disagree.

        The core sees a non-NULL pointer — a file that exists and could not be
        measured — and uploads it. The in-tree kernel's empty-slot branch tests
        the dict for truthiness, so an empty one reads as no file at all. The
        core's reading is the correct one; the executor cannot produce this
        shape (``_build_local_input`` always returns filename + path), so the
        divergence is unreachable in production and excluded from the
        differential below.
        """
        assert adapter.compute_sync_action({}, [], {}, _DEVICE, None) == Upload(target_save_id=None)
        assert compute_sync_action({}, [], {}, _DEVICE, None) == Skip(reason="nothing_to_sync")


class TestDecisionTableDifferential:
    def test_adapter_matches_python_kernel_over_crossed_inputs(self, adapter: GavelNativeAdapter) -> None:
        """The two kernels agree on every crossed input the executor can produce.

        Local-file forms (missing, unmeasurable, 0-byte, shrunk, both mtime
        directions) crossed with all three ``device_syncs`` branches, parseable
        and unparseable server timestamps, and every baseline combination —
        including the ``None`` and ``""`` spellings of "unknown" on each hash.
        """
        mismatches: list[str] = []
        compared = 0
        for (
            local_name,
            slot_name,
            local_hash,
            last_sync_hash,
            last_sync_server_hash,
            last_sync_local_size,
        ) in itertools.product(
            _LOCAL_FILES,
            _SLOTS,
            _LOCAL_HASHES,
            _LAST_SYNC_HASHES,
            _LAST_SYNC_SERVER_HASHES,
            _LAST_SYNC_LOCAL_SIZES,
        ):
            # The one known divergence, pinned by
            # TestDecisionTable.test_empty_local_file_dict_on_an_empty_slot_is_the_one_known_divergence.
            if local_name == "empty-dict" and slot_name == "empty":
                continue
            local_file = _LOCAL_FILES[local_name]
            slot = _SLOTS[slot_name]
            files_state = {
                "last_sync_hash": last_sync_hash,
                "last_sync_server_hash": last_sync_server_hash,
                "last_sync_local_size": last_sync_local_size,
            }
            native: SyncAction = adapter.compute_sync_action(local_file, slot, files_state, _DEVICE, local_hash)
            oracle = compute_sync_action(local_file, slot, files_state, _DEVICE, local_hash)
            compared += 1
            if native != oracle:
                mismatches.append(
                    f"local={local_name} slot={slot_name} local_hash={local_hash!r} "
                    f"last_sync_hash={last_sync_hash!r} last_sync_server_hash={last_sync_server_hash!r} "
                    f"last_sync_local_size={last_sync_local_size!r}: native={native!r} oracle={oracle!r}"
                )
        assert not mismatches, "native core diverged from the Python kernel:\n" + "\n".join(mismatches)
        # A shrunken product would make the agreement above vacuous, and nothing
        # else would say so. The literal is the point: deriving it from the same
        # collections that drive the product would hold for any dimension size,
        # which is exactly the failure this is meant to catch. 7 local files x 8
        # slots x 5 local hashes x 3 baselines x 3 server baselines x 4 baseline
        # sizes, less the excluded (empty-dict, empty slot) slice.
        assert compared == 9900
