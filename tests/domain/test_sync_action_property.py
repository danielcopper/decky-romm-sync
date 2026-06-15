"""Property-based tests for ``domain.sync_action.compute_sync_action`` (#1028).

The save-sync decision kernel is pure and safety-critical: a wrong action can
overwrite or lose a player's save. Hand-enumerated cases
(``test_sync_action.py``) pin specific input shapes; these properties state the
*invariants* the kernel must hold across the whole input space the generators
sample.

Property-test convention — pinning open bugs
---------------------------------------------
A property states the TRUE invariant. If it FAILS today, the invariant's bug
is still open, so the property is pinned ``@pytest.mark.xfail(strict=True,
reason="#<issue>: …")``. ``strict=True`` means the day the fix lands the
property passes → the run reports XPASS → CI fails → the marker must be
removed, and the property then guards against regression. Never weaken a
property to make it pass.

Invariants encoded here:
- Inv2 (#965): no kernel output destroys a present local file without the
  carried ``server_save`` being the recovery source.
- Inv3 (#1014): the action is identical under semantically-equal
  ``updated_at`` / ``mtime`` ISO renderings.
- Inv4 (#1013): same inputs replayed → same action (pure determinism), and
  after a first-sync ``Upload(None)`` baseline is adopted the next run is
  ``Skip("synced")`` — never another POST.
- Inv5 (#1059): branch-6 divergence from a held baseline is always a
  ``Conflict`` — never a silent ``Download``/``Upload``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    SyncAction,
    Upload,
    compute_sync_action,
)

DEVICE_ID = "device-abc"
OTHER_DEVICE_ID = "device-xyz"

# A bounded epoch window (year 2000-2099) so both local mtimes and server
# updated_at instants are realistic and comparable.
_MIN_EPOCH = datetime(2000, 1, 1, tzinfo=UTC).timestamp()
_MAX_EPOCH = datetime(2099, 12, 31, tzinfo=UTC).timestamp()

_epochs = st.floats(min_value=_MIN_EPOCH, max_value=_MAX_EPOCH, allow_nan=False, allow_infinity=False)

# Hashes are opaque MD5-shaped tokens; the kernel only compares them for
# equality, so a small alphabet keeps generation cheap while still exercising
# both the equal and diverged branches.
_hashes = st.sampled_from(["hash-a", "hash-b", "hash-c"])
_opt_hashes = st.none() | _hashes


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
    return {
        "id": draw(st.integers(min_value=1, max_value=9999)),
        "slot": 0,
        "updated_at": _epoch_to_iso(epoch, zulu=draw(st.booleans()), micros=draw(st.booleans())),
        "file_extension": "srm",
        "device_syncs": draw(_device_syncs()),
    }


_server_lists = st.lists(_server_saves(), min_size=0, max_size=5)
_files_states = st.fixed_dictionaries({}, optional={"last_sync_hash": _hashes})


def _action(
    local_file: dict[str, Any] | None,
    server_saves: list[dict[str, Any]],
    files_state: dict[str, Any],
    local_hash: str | None,
) -> SyncAction:
    return compute_sync_action(
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
    IDENTICAL ``SyncAction``. The kernel orders by ``parse_iso_to_epoch``, so
    format must not influence the decision — the #1014 class (lexicographic
    ordering) at the kernel surface.
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


@given(local_file=_local_files(), server_epoch=_epochs, local_hash=_hashes)
def test_idempotent_after_branch6_upload_and_baseline_adoption(
    local_file: dict[str, Any],
    server_epoch: float,
    local_hash: str,
) -> None:
    """Branch-6 → adopt → Skip replay invariance (the #1013 no-loop property).

    Step 1: a save with no ``device_syncs`` entry for our device and a local
    mtime at-or-after the server's ``updated_at`` dispatches ``Upload(None)``
    (POST a new save).

    Step 2: the service adopts the baseline — the server save now carries our
    ``device_syncs`` entry with ``is_current=True`` and
    ``files_state["last_sync_hash"]`` equals the local hash. Re-running the
    kernel on those updated inputs MUST return ``Skip("synced")``, never
    another ``Upload`` — otherwise sync churns out duplicate server saves on
    every pass.
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

    step1 = _action(local_file, [server], {}, local_hash)
    assume(isinstance(step1, Upload) and step1.target_save_id is None)

    # Service adopts baseline: our device entry is now current, and the
    # recorded baseline hash matches the local content.
    adopted_server = {
        **server,
        "device_syncs": [{"device_id": DEVICE_ID, "is_current": True}],
    }
    step2 = _action(local_file, [adopted_server], {"last_sync_hash": local_hash}, local_hash)
    assert step2 == Skip(reason="synced")


# ---------------------------------------------------------------------------
# Invariant 5 (#1059): branch-6 divergence from baseline is always a Conflict.
# ---------------------------------------------------------------------------


@given(local_file=_local_files(), server_epoch=_epochs, local_hash=_hashes, baseline=_hashes)
def test_no_entry_diverged_baseline_is_conflict(
    local_file: dict[str, Any],
    server_epoch: float,
    local_hash: str,
    baseline: str,
) -> None:
    """Branch 6 / #1059 — when the newest server save has no ``device_syncs``
    entry for our device, we hold a baseline (``last_sync_hash``), and the
    present local file has diverged from that baseline
    (``local_hash != last_sync_hash``), the kernel returns ``Conflict`` — never
    a silent ``Download`` (server replaces diverged local) or ``Upload`` (local
    replaces a head we never synced). Both sides moved: the chosen head is a
    save we never synced while local drifted offline. Holds regardless of the
    mtime ordering between local and the server save.
    """
    assume(local_hash != baseline)
    server = {
        "id": 7,
        "slot": 0,
        "updated_at": _epoch_to_iso(server_epoch, zulu=False, micros=False),
        "file_extension": "srm",
        "device_syncs": [{"device_id": OTHER_DEVICE_ID, "is_current": True}],
    }
    result = _action(local_file, [server], {"last_sync_hash": baseline}, local_hash)
    assert result == Conflict(server_save=server)


@pytest.mark.xfail(
    strict=True,
    reason="#1013: branch 6 ignores content hashes — byte-identical content with no device entry POSTs a duplicate",
)
@given(local_file=_local_files(), server_epoch=_epochs, local_hash=_hashes)
def test_no_entry_identical_content_does_not_duplicate(
    local_file: dict[str, Any],
    server_epoch: float,
    local_hash: str,
) -> None:
    """No device entry + content byte-identical to the slot's server save must
    NOT POST a duplicate (the #1013 invariant).

    The slot already holds a server save whose content hash equals the local
    content (``content_hash == local_hash`` — a copied SD card, a restored
    backup, a fresh reinstall). Our device has no ``device_syncs`` entry, and
    the local mtime is at-or-after the server's ``updated_at``. The correct
    action adopts the existing server save as the baseline
    (``Skip(adopt_baseline=True)``); POSTing a second copy of identical bytes
    creates a duplicate server save and churns autocleanup.

    Today branch 6 (`_decide_when_no_entry`) compares only mtime and returns
    ``Upload(target_save_id=None)`` — this property fails until #1013 threads
    the server content hash into the decision. When the fix lands the property
    XPASSes, ``strict=True`` flips the run red, the marker is removed, and the
    property guards against the duplicate-POST regression thereafter.
    """
    local_file = {**local_file, "mtime": server_epoch + 3600}
    server = {
        "id": 7,
        "slot": 0,
        "updated_at": _epoch_to_iso(server_epoch, zulu=False, micros=False),
        "file_extension": "srm",
        "content_hash": local_hash,
        "device_syncs": [{"device_id": OTHER_DEVICE_ID, "is_current": True}],
    }
    result = _action(local_file, [server], {}, local_hash)
    # Identical content already on the server → adopt it, never POST a duplicate.
    assert result == Skip(reason="synced", adopt_baseline=True)
