"""Property-based tests for the compiled save-sync decision core (#1028).

The decision is safety-critical: a wrong action can overwrite or lose a
player's save. It is made by the vendored gavel core, reached through
:class:`~adapters.gavel_native.GavelNativeAdapter`, so these properties run
against the adapter — the production path.

What earns a place in this tier
-------------------------------
The vendored gavel vectors already bind single inputs to single outputs, which
is exactly what a point statement about one row of the decision table needs.
A property is worth its cost only where a vector cannot reach:

- it quantifies over a space the vectors do not exhaust (every action the core
  can emit, over arbitrary slots and bookkeeping), or
- it relates several runs to each other — the same decision under two ISO
  renderings, a replay, a state sequence. One input bound to one output can
  never say that.

Properties that restated a single table row were dropped when the core took
over the decision; the vectors carry those, and stating them twice is two
places to maintain for one rule.

Property-test convention — pinning open bugs
---------------------------------------------
A property states the TRUE invariant. If it FAILS today, the invariant's bug
is still open, so the property is pinned ``@pytest.mark.xfail(strict=True,
reason="#<issue>: …")``. ``strict=True`` means the day the fix lands the
property passes → the run reports XPASS → CI fails → the marker must be
removed, and the property then guards against regression. Never weaken a
property to make it pass.

Invariants encoded here:
- Inv2 (#965): no output destroys a present local file without the carried
  ``server_save`` being the recovery source.
- Inv3 (#1014): the action is identical under semantically-equal
  ``updated_at`` / ``mtime`` ISO renderings.
- Inv4 (#1013): same inputs replayed → same action (pure determinism); after a
  first-sync ``Upload(None)`` baseline is adopted the next run is
  ``Skip("synced")`` — never another POST.
- Inv11 (#1480): whenever ``local_hash`` and the picked head's ``content_hash``
  are both truthy and equal (byte-identical content), the decision is never
  ``Conflict`` — the row-12 split (12a).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from adapters.gavel_native import GavelNativeAdapter
from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    SyncAction,
    Upload,
)

_ADAPTER = GavelNativeAdapter()

DEVICE_ID = "device-abc"
OTHER_DEVICE_ID = "device-xyz"

# A bounded epoch window (year 2000-2099) so both local mtimes and server
# updated_at instants are realistic and comparable.
_MIN_EPOCH = datetime(2000, 1, 1, tzinfo=UTC).timestamp()
_MAX_EPOCH = datetime(2099, 12, 31, tzinfo=UTC).timestamp()

_epochs = st.floats(min_value=_MIN_EPOCH, max_value=_MAX_EPOCH, allow_nan=False, allow_infinity=False)

# Hashes are opaque MD5-shaped tokens; the kernel only compares them for
# equality, so a small alphabet keeps generation cheap while still exercising
# both the equal and diverged branches. The empty string sits alongside them: it
# is how a hash column persisted blank comes back, and the adapter forwards it as
# a distinct empty value rather than collapsing it to NULL, so the space a
# property quantifies over has to contain it.
_hashes = st.sampled_from(["", "hash-a", "hash-b", "hash-c"])
_opt_hashes = st.none() | _hashes

# The subset a property draws from when its own premise names a real hash — a
# baseline that is *held*, content that is *provably* identical. An empty hash
# proves neither, so drawing one there would generate inputs outside what the
# property claims rather than widen what it covers.
_truthy_hashes = st.sampled_from(["hash-a", "hash-b", "hash-c"])


def _epoch_to_iso(epoch: float, *, zulu: bool, micros: bool) -> str:
    """Render an epoch as one of the ISO shapes RomM has emitted."""
    dt = datetime.fromtimestamp(epoch, tz=UTC).replace(microsecond=0)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    if micros:
        base = f"{base}.000000"
    return f"{base}Z" if zulu else f"{base}+00:00"


@st.composite
def _local_files(draw: st.DrawFn) -> dict[str, Any]:
    mtime = draw(_epochs)
    return {
        "filename": "Game.srm",
        "path": "/tmp/Game.srm",
        "size": draw(st.integers(min_value=0, max_value=1_048_576)),
        "mtime": mtime,
    }


_opt_local_files = st.none() | _local_files()


@st.composite
def _device_syncs(draw: st.DrawFn) -> list[dict[str, Any]]:
    """A device_syncs list that may include our device, another device, both,
    or neither — exercising all three branches (is_current, not-current,
    no-entry)."""
    entries: list[dict[str, Any]] = []
    if draw(st.booleans()):
        entries.append({"device_id": DEVICE_ID, "is_current": draw(st.booleans())})
    if draw(st.booleans()):
        entries.append({"device_id": OTHER_DEVICE_ID, "is_current": draw(st.booleans())})
    return entries


@st.composite
def _server_saves(draw: st.DrawFn) -> dict[str, Any]:
    epoch = draw(_epochs)
    save: dict[str, Any] = {
        "id": draw(st.integers(min_value=1, max_value=9999)),
        "slot": 0,
        "updated_at": _epoch_to_iso(epoch, zulu=draw(st.booleans()), micros=draw(st.booleans())),
        "file_extension": "srm",
        "device_syncs": draw(_device_syncs()),
    }
    # RomM stamps most saves with a ``content_hash`` (mirrors ``_hashes``); older
    # / migrated saves may omit it, so ``None`` leaves the key absent.
    content_hash = draw(_opt_hashes)
    if content_hash is not None:
        save["content_hash"] = content_hash
    return save


_server_lists = st.lists(_server_saves(), min_size=0, max_size=5)
_sizes = st.integers(min_value=0, max_value=1_048_576)
_files_states = st.fixed_dictionaries(
    {},
    optional={"last_sync_hash": _hashes, "last_sync_server_hash": _hashes, "last_sync_local_size": _sizes},
)


def _action(
    local_file: dict[str, Any] | None,
    server_saves: list[dict[str, Any]],
    files_state: dict[str, Any],
    local_hash: str | None,
) -> SyncAction:
    return _ADAPTER.compute_sync_action(
        local_file=local_file,
        server_saves_in_slot=server_saves,
        files_state=files_state,
        device_id=DEVICE_ID,
        local_hash=local_hash,
    )


# ---------------------------------------------------------------------------
# Invariant 2 (#965): no destructive action without a recovery source.
# ---------------------------------------------------------------------------


@given(
    local_file=_opt_local_files,
    server_saves=_server_lists,
    files_state=_files_states,
    local_hash=_opt_hashes,
)
def test_no_destructive_action_without_recovery(
    local_file: dict[str, Any] | None,
    server_saves: list[dict[str, Any]],
    files_state: dict[str, Any],
    local_hash: str | None,
) -> None:
    """The kernel never emits an action that loses local data without a
    recovery source.

    Concretely, for any inputs:
    - ``Skip("nothing_to_sync")`` only when there is no local file (nothing was
      lost — there was nothing to protect).
    - ``Download`` / ``Conflict`` always carry a ``server_save`` drawn from the
      input slot — so when they overwrite a present local file, the bytes that
      replace it are a real server record (the recovery source), never a
      fabricated or empty one.
    - ``Upload`` never targets a save id absent from the slot.

    This is the kernel-output half of the #965 class (never delete local data
    that has neither a server copy nor a backup). The destructive *deletion*
    path #965 reports lives in the slot-switch service, not in this pure
    kernel; here we pin that the kernel itself never originates such an action.
    """
    result = _action(local_file, server_saves, files_state, local_hash)
    slot_ids = {ss.get("id") for ss in server_saves}
    slot_identities = {id(ss) for ss in server_saves}

    if isinstance(result, Skip) and result.reason == "nothing_to_sync":
        assert local_file is None
    elif isinstance(result, (Download, Conflict)):
        # The carried save must be one of the actual server records in the
        # slot — the recovery source, not a fabricated dict.
        assert id(result.server_save) in slot_identities
    elif isinstance(result, Upload) and result.target_save_id is not None:
        # POST (None) is always allowed; a PUT must target a real slot save.
        assert result.target_save_id in slot_ids


# ---------------------------------------------------------------------------
# Invariant 3 (#1014): decisions stable under timestamp-format variation.
# ---------------------------------------------------------------------------


def _reformat_server(ss: dict[str, Any], *, zulu: bool, micros: bool) -> dict[str, Any]:
    """Return a copy of *ss* with ``updated_at`` re-rendered in another
    semantically-equal ISO shape."""
    epoch = datetime.fromisoformat(ss["updated_at"].replace("Z", "+00:00")).timestamp()
    return {**ss, "updated_at": _epoch_to_iso(epoch, zulu=zulu, micros=micros)}


@given(
    local_file=_opt_local_files,
    server_saves=_server_lists,
    files_state=_files_states,
    local_hash=_opt_hashes,
    zulu=st.booleans(),
    micros=st.booleans(),
)
def test_action_stable_under_timestamp_format(
    local_file: dict[str, Any] | None,
    server_saves: list[dict[str, Any]],
    files_state: dict[str, Any],
    local_hash: str | None,
    zulu: bool,
    micros: bool,
) -> None:
    """Re-rendering every ``updated_at`` in a different but semantically-equal
    ISO shape (``Z`` ⇄ ``+00:00``, with/without microseconds) yields an
    IDENTICAL ``SyncAction``. Head selection runs on the epoch the adapter parses
    out (``parse_iso_to_epoch``), never on the raw string, so format must not
    influence the decision — the #1014 class (lexicographic ordering) at the
    decision surface.
    """
    base = _action(local_file, server_saves, files_state, local_hash)
    reformatted = [_reformat_server(ss, zulu=zulu, micros=micros) for ss in server_saves]
    other = _action(local_file, reformatted, files_state, local_hash)

    # Compare by value. Download/Conflict carry the server dict, which differs
    # only in updated_at formatting between the two runs; compare structurally
    # on everything except that one field. Skip/Upload carry no timestamp, so
    # they must be value-equal outright.
    assert type(base) is type(other)
    if isinstance(base, (Download, Conflict)):
        other_save = other.server_save  # type: ignore[union-attr]
        picked = base.server_save
        assert picked["id"] == other_save["id"]
        epoch_picked = datetime.fromisoformat(picked["updated_at"].replace("Z", "+00:00")).timestamp()
        epoch_other = datetime.fromisoformat(other_save["updated_at"].replace("Z", "+00:00")).timestamp()
        assert epoch_picked == epoch_other
    else:
        assert base == other


# ---------------------------------------------------------------------------
# Invariant 4 (#1013): replay determinism + idempotence after baseline adoption.
# ---------------------------------------------------------------------------


@given(
    local_file=_opt_local_files,
    server_saves=_server_lists,
    files_state=_files_states,
    local_hash=_opt_hashes,
)
def test_replay_determinism(
    local_file: dict[str, Any] | None,
    server_saves: list[dict[str, Any]],
    files_state: dict[str, Any],
    local_hash: str | None,
) -> None:
    """The kernel is a pure function: identical inputs replayed yield an
    identical action. The foundation of the no-loop guarantee."""
    first = _action(local_file, server_saves, files_state, local_hash)
    second = _action(local_file, server_saves, files_state, local_hash)
    assert first == second


@given(local_file=_local_files(), server_epoch=_epochs, local_hash=_truthy_hashes)
def test_idempotent_after_branch6_upload_and_baseline_adoption(
    local_file: dict[str, Any],
    server_epoch: float,
    local_hash: str,
) -> None:
    """Branch-6 → adopt → Skip replay invariance (the #1013 no-loop property).

    Step 1: a save with no ``device_syncs`` entry for our device, a held baseline
    the local still matches (``last_sync_hash == local_hash`` — unchanged since
    our last sync, so #1276 does NOT route it to a conflict), a server head with
    no ``content_hash`` to dedup against, and a local mtime at-or-after the
    server's ``updated_at`` dispatches ``Upload(None)`` (POST a new save over a
    head we never synced).

    Step 2: the service adopts the baseline — the server save now carries our
    ``device_syncs`` entry with ``is_current=True`` and
    ``files_state["last_sync_hash"]`` still equals the local hash. Re-running the
    kernel on those updated inputs MUST return ``Skip("synced")``, never another
    ``Upload`` — otherwise sync churns out duplicate server saves on every pass.
    """
    # Local mtime at-or-after server updated_at so step 1 is the POST branch.
    local_file = {**local_file, "mtime": server_epoch + 3600}
    server = {
        "id": 7,
        "slot": 0,
        "updated_at": _epoch_to_iso(server_epoch, zulu=False, micros=False),
        "file_extension": "srm",
        "device_syncs": [{"device_id": OTHER_DEVICE_ID, "is_current": True}],
    }

    # Local unchanged since our last sync (baseline matches) → the #1276 guard
    # does not fire, so branch 6 still POSTs a new version by mtime.
    files_state = {"last_sync_hash": local_hash}
    step1 = _action(local_file, [server], files_state, local_hash)
    assert step1 == Upload(target_save_id=None)

    # Service adopts baseline: our device entry is now current, and the
    # recorded baseline hash matches the local content.
    adopted_server = {
        **server,
        "device_syncs": [{"device_id": DEVICE_ID, "is_current": True}],
    }
    step2 = _action(local_file, [adopted_server], files_state, local_hash)
    assert step2 == Skip(reason="synced")


# ---------------------------------------------------------------------------
# Invariant 11 (#1480): byte-identical content never surfaces a Conflict.
# ---------------------------------------------------------------------------


@st.composite
def _identical_content_scenario(draw: st.DrawFn) -> dict[str, Any]:
    """A present local plus a SINGLE server head whose ``content_hash`` EQUALS the
    truthy ``local_hash`` — byte-identical by construction — with arbitrary
    ``device_syncs`` (all three branches: our device current / not-current /
    absent) and an arbitrary baseline.

    A single head keeps ``max(updated_at)`` selection unambiguous, so the invariant
    can be stated against *the* picked head without replaying the pick logic here.

    The local is never empty and never smaller than its recorded baseline size, so
    the branch-4 corrupt-local guard (#1062 / row 9b) — the one case where
    identical content may still be refused, on size grounds orthogonal to content
    identity — cannot fire on any sampled example. Keeping it out of the space is
    what lets the property say "never a Conflict" outright; restating the guard's
    shrink threshold here would put a second copy of a rule this tier does not own.
    """
    local_hash = draw(_truthy_hashes)
    files_state: dict[str, Any] = draw(_files_states)
    baseline_size: int = files_state.get("last_sync_local_size", 0)
    local_file = {
        "filename": "Game.srm",
        "path": "/tmp/Game.srm",
        "size": draw(st.integers(min_value=max(1, baseline_size), max_value=1_048_576)),
        "mtime": draw(_epochs),
    }
    device_syncs: list[dict[str, Any]] = []
    if draw(st.booleans()):
        device_syncs.append({"device_id": DEVICE_ID, "is_current": draw(st.booleans())})
    if draw(st.booleans()):
        device_syncs.append({"device_id": OTHER_DEVICE_ID, "is_current": draw(st.booleans())})
    server = {
        "id": draw(st.integers(min_value=1, max_value=9999)),
        "slot": 0,
        "updated_at": _epoch_to_iso(draw(_epochs), zulu=draw(st.booleans()), micros=draw(st.booleans())),
        "file_extension": "srm",
        "content_hash": local_hash,  # byte-identical by construction
        "device_syncs": device_syncs,
    }
    return {
        "local_file": local_file,
        "server": server,
        "files_state": files_state,
        "local_hash": local_hash,
    }


@given(scenario=_identical_content_scenario())
def test_identical_content_never_conflicts(scenario: dict[str, Any]) -> None:
    """Branch 5 / #1480 (also re-verified at branches 6 and 5's no-baseline
    slice) — whenever ``local_hash`` and the picked head's ``content_hash`` are
    both truthy and EQUAL, and the local carries no sign of corruption, the
    decision is never ``Conflict``. Two independent moves that land on identical
    bytes have nothing to reconcile, so the safe outcome is always a ``Download``
    (adopt the head) or a ``Skip`` (adopt the baseline) — the row-12 split (12a)
    stated directly, across every ``device_syncs`` branch and baseline the
    generator reaches.
    """
    result = _action(
        scenario["local_file"],
        [scenario["server"]],
        scenario["files_state"],
        scenario["local_hash"],
    )

    assert not isinstance(result, Conflict)
