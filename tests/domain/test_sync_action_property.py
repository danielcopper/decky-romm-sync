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
- Inv4 (#1013): same inputs replayed → same action (pure determinism); after a
  first-sync ``Upload(None)`` baseline is adopted the next run is
  ``Skip("synced")`` — never another POST; and a branch-6 save whose
  server-provided ``content_hash`` already equals the local content is adopted
  as the baseline (``Skip(adopt_baseline=True)``) rather than re-POSTed. Now
  enforced live (no longer xfail-pinned).
- Inv5 (#1059): branch-6 divergence from a held baseline is always a
  ``Conflict`` — never a silent ``Download``/``Upload``.
- Inv6 (#1062): branch-4 (is_current=true) never emits an in-place PUT for a
  0-byte / implausibly-shrunken diverged local save — that would overwrite the
  only good server copy with no recoverable version; it is a ``Conflict``.
- Inv7 (#1276): branch-5 (is_current=false) with a present local and NO recorded
  baseline never returns ``Download`` unless the local content is provably
  byte-identical to the chosen server save (``local_hash ==
  server.content_hash``) — otherwise it is a ``Conflict``, never a silent
  overwrite of an unbacked local edit.
- Inv8 (#1276): ``resolve_upload_conflict`` (the 409 write-time backstop) never
  returns ``"download"`` unless ``local_hash`` is non-None and equal to the
  recorded baseline or the server's current content; a missing ``local_hash``
  always yields ``"conflict"``.
- Inv9 (#1276): branch-6 (no ``device_syncs`` entry for our device) with a
  present local and NO recorded baseline returns ``Conflict`` whenever the local
  is not byte-identical to the chosen head — never a silent mtime-based
  ``Download`` / ``Upload`` of an unbacked local edit; a byte-identical local is
  adopted (``Skip(adopt_baseline=True)``), never a duplicate POST. Branch-5
  parity.
- Inv10 (#1468): branch-6 adopts the baseline (``Skip(adopt_baseline=True)``)
  ONLY when the local is proven byte-identical to the head — either the stored
  server hash matches while local is unchanged since baseline (provenance) or the
  live parity hash matches (fallback). The provenance route never fires when
  local has diverged from the baseline, so a stored server hash can never
  fabricate a false identity for changed local content.
- Inv11 (#1480): whenever ``local_hash`` and the picked head's ``content_hash``
  are both truthy and equal (byte-identical content), the kernel never returns
  ``Conflict`` — the row-12 split (12a). The sole carve-out is the branch-4
  size-corruption guard (#1062 / row 9b), which is orthogonal to content
  identity: a truncated local that happens to hash-match an equally-truncated
  head is still refused in place.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from hypothesis import assume, given
from hypothesis import strategies as st

from domain.save_size import is_implausibly_shrunken
from domain.sync_action import (
    Conflict,
    Download,
    Skip,
    SyncAction,
    Upload,
    compute_sync_action,
    resolve_upload_conflict,
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
    creates a duplicate server save and churns autocleanup. Guards against the
    duplicate-POST regression.
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


# ---------------------------------------------------------------------------
# Invariant 6 (#1062): branch-4 never PUTs a 0-byte / shrunken local in place.
# ---------------------------------------------------------------------------


@given(
    server_epoch=_epochs,
    local_hash=_hashes,
    baseline=_hashes,
    new_size=st.integers(min_value=0, max_value=1_048_576),
    baseline_size=st.one_of(st.none(), st.integers(min_value=0, max_value=1_048_576)),
)
def test_is_current_implausible_local_never_puts_in_place(
    server_epoch: float,
    local_hash: str,
    baseline: str,
    new_size: int,
    baseline_size: int | None,
) -> None:
    """Branch 4 / #1062 — when our device is ``is_current=true`` on the picked
    save, the present local has diverged from the held baseline, AND the local
    size is implausible (0-byte or shrunk past the threshold versus the recorded
    baseline size), the kernel returns ``Conflict`` — never ``Upload`` (an
    in-place PUT that RomM applies with no recoverable version, destroying the
    only good copy). The safety invariant stated directly: no in-place overwrite
    of a server save with a corrupt-looking local file.
    """
    assume(local_hash != baseline)
    local_file = {
        "filename": "Game.srm",
        "path": "/tmp/Game.srm",
        "size": new_size,
        "mtime": server_epoch,
    }
    files_state: dict[str, Any] = {"last_sync_hash": baseline}
    if baseline_size is not None:
        files_state["last_sync_local_size"] = baseline_size
    server = {
        "id": 7,
        "slot": 0,
        "updated_at": _epoch_to_iso(server_epoch, zulu=False, micros=False),
        "file_extension": "srm",
        "device_syncs": [{"device_id": DEVICE_ID, "is_current": True}],
    }

    result = _action(local_file, [server], files_state, local_hash)

    if is_implausibly_shrunken(new_size, baseline_size):
        # Corrupt-looking local → never an in-place PUT; route to the user.
        assert result == Conflict(server_save=server)
    else:
        # Plausible divergent edit → the normal in-place PUT.
        assert result == Upload(target_save_id=7)


# ---------------------------------------------------------------------------
# Invariant 7 (#1276): branch-5 no-baseline downloads only on proven identity.
# ---------------------------------------------------------------------------


@st.composite
def _head_not_current_no_baseline(draw: st.DrawFn) -> dict[str, Any]:
    """A single server head on which our device is present but is_current=False.

    Built directly (not by assume-filtering a random list) so the property never
    trips Hypothesis's ``filter_too_much`` health check — every generated example
    lands squarely in the branch-5 no-baseline slice.
    """
    server: dict[str, Any] = {
        "id": draw(st.integers(min_value=1, max_value=9999)),
        "slot": 0,
        "updated_at": _epoch_to_iso(draw(_epochs), zulu=draw(st.booleans()), micros=draw(st.booleans())),
        "device_syncs": [{"device_id": DEVICE_ID, "is_current": False}],
    }
    content_hash = draw(_opt_hashes)
    if content_hash is not None:
        server["content_hash"] = content_hash
    return server


@given(
    local_file=_local_files(),
    server=_head_not_current_no_baseline(),
    local_hash=_opt_hashes,
)
def test_not_current_no_baseline_downloads_only_when_content_identical(
    local_file: dict[str, Any],
    server: dict[str, Any],
    local_hash: str | None,
) -> None:
    """Branch 5 / #1276 — our device is present but ``is_current=false`` on the
    chosen server head, a local file is present, and we hold NO recorded baseline
    (``files_state`` has no ``last_sync_hash``). In that slice the kernel returns
    ``Download`` ONLY when the local content is provably byte-identical to the
    chosen save (``local_hash == server.content_hash``); every other case is a
    ``Conflict``, never a silent overwrite of an unbacked local edit. This is the
    row-11 fix stated directly.
    """
    result = _action(local_file, [server], {}, local_hash)

    if isinstance(result, Download):
        assert local_hash is not None
        assert server.get("content_hash") == local_hash


# ---------------------------------------------------------------------------
# Invariant 8 (#1276): the 409 backstop never downloads unless provably unchanged.
# ---------------------------------------------------------------------------


@given(
    local_hash=_opt_hashes,
    last_sync_hash=_opt_hashes,
    server_content_hash=_opt_hashes,
    last_sync_server_hash=_opt_hashes,
)
def test_resolve_upload_conflict_never_downloads_unless_provably_unchanged(
    local_hash: str | None,
    last_sync_hash: str | None,
    server_content_hash: str | None,
    last_sync_server_hash: str | None,
) -> None:
    """Branch 409 / #1276 + #1468 — ``resolve_upload_conflict`` maps a write-time
    409 to an action. It returns ``"download"`` (adopt the server) ONLY when
    ``local_hash`` is non-None and equals either our recorded baseline
    (``last_sync_hash``) or the server's current content (``server_content_hash``);
    a missing ``local_hash`` always yields ``"conflict"``. A stored server hash
    (the #1468 provenance input) never enables a download outside those two
    equalities — provenance requires ``local_hash == last_sync_hash``, already
    covered. Missing evidence never downgrades to a data-losing download.
    """
    result = resolve_upload_conflict(local_hash, last_sync_hash, server_content_hash, last_sync_server_hash)
    assert result in ("download", "conflict")

    if local_hash is None:
        assert result == "conflict"

    if result == "download":
        assert local_hash is not None
        assert local_hash in (last_sync_hash, server_content_hash)


# ---------------------------------------------------------------------------
# Invariant 9 (#1276): branch-6 no-baseline differing local is always a Conflict.
# ---------------------------------------------------------------------------


@st.composite
def _head_no_entry(draw: st.DrawFn) -> dict[str, Any]:
    """A single server head with NO ``device_syncs`` entry for our device.

    Built directly (not by assume-filtering a random list) so the property never
    trips Hypothesis's ``filter_too_much`` health check — every generated example
    lands squarely in the branch-6 slice. Our device is absent from
    ``device_syncs`` (empty, or only the other device present).
    """
    device_syncs: list[dict[str, Any]] = []
    if draw(st.booleans()):
        device_syncs.append({"device_id": OTHER_DEVICE_ID, "is_current": draw(st.booleans())})
    server: dict[str, Any] = {
        "id": draw(st.integers(min_value=1, max_value=9999)),
        "slot": 0,
        "updated_at": _epoch_to_iso(draw(_epochs), zulu=draw(st.booleans()), micros=draw(st.booleans())),
        "file_extension": "srm",
        "device_syncs": device_syncs,
    }
    content_hash = draw(_opt_hashes)
    if content_hash is not None:
        server["content_hash"] = content_hash
    return server


@given(
    local_file=_local_files(),
    server=_head_no_entry(),
    local_hash=_hashes,
)
def test_no_entry_no_baseline_differing_local_is_conflict(
    local_file: dict[str, Any],
    server: dict[str, Any],
    local_hash: str,
) -> None:
    """Branch 6 / #1276 — no ``device_syncs`` entry for our device, a present
    local with a real hash, and NO recorded baseline (``files_state`` has no
    ``last_sync_hash``). When the local content is not byte-identical to the
    chosen head (``local_hash != server.content_hash``, or the head carries no
    ``content_hash``) the kernel returns ``Conflict`` — never a silent
    mtime-based ``Download`` or ``Upload`` of an unbacked local edit. When it IS
    byte-identical the head is adopted (``Skip(adopt_baseline=True)``), never a
    duplicate POST. Branch-5 parity, stated directly.
    """
    result = _action(local_file, [server], {}, local_hash)

    if server.get("content_hash") == local_hash:
        assert result == Skip(reason="synced", adopt_baseline=True)
    else:
        assert result == Conflict(server_save=server)


# ---------------------------------------------------------------------------
# Invariant 10 (#1468): branch-6 adopts only on proven identity; provenance
# never fires on a diverged local.
# ---------------------------------------------------------------------------


@given(
    local_file=_local_files(),
    server=_head_no_entry(),
    local_hash=_hashes,
    baseline=_opt_hashes,
    stored_server_hash=_opt_hashes,
)
def test_no_entry_adopts_baseline_only_on_proven_identity(
    local_file: dict[str, Any],
    server: dict[str, Any],
    local_hash: str,
    baseline: str | None,
    stored_server_hash: str | None,
) -> None:
    """Branch 6 / #1468 — with a present local and NO ``device_syncs`` entry for
    our device, the kernel adopts the head as the baseline
    (``Skip(adopt_baseline=True)``) ONLY when the local is proven byte-identical
    to it: either the stored server hash matches the head's ``content_hash`` while
    local is unchanged since our baseline (provenance) OR the live parity hash
    matches (``local_hash == server.content_hash``). Crucially, the provenance
    route never fires on a local that has diverged from the baseline
    (``local_hash != last_sync_hash``) — a stored server hash can never fabricate
    a false identity for changed local content.
    """
    files_state: dict[str, Any] = {}
    if baseline is not None:
        files_state["last_sync_hash"] = baseline
    if stored_server_hash is not None:
        files_state["last_sync_server_hash"] = stored_server_hash

    result = _action(local_file, [server], files_state, local_hash)

    server_hash = server.get("content_hash")
    parity = server_hash is not None and local_hash == server_hash
    provenance = (
        baseline is not None
        and local_hash == baseline
        and bool(stored_server_hash)
        and stored_server_hash == server_hash
    )

    if isinstance(result, Skip) and result.adopt_baseline:
        assert parity or provenance

    # The literal safety statement: provenance can never rescue a diverged local.
    if baseline is not None and local_hash != baseline and not parity:
        assert not (isinstance(result, Skip) and result.adopt_baseline)


# ---------------------------------------------------------------------------
# Invariant 11 (#1480): byte-identical content never surfaces a Conflict
# (except the orthogonal branch-4 size-corruption guard).
# ---------------------------------------------------------------------------


@st.composite
def _identical_content_scenario(draw: st.DrawFn) -> dict[str, Any]:
    """A present local plus a SINGLE server head whose ``content_hash`` EQUALS the
    truthy ``local_hash`` — byte-identical by construction — with arbitrary
    ``device_syncs`` (all three branches: our device current / not-current /
    absent), an arbitrary baseline, and arbitrary sizes.

    A single head keeps ``max(updated_at)`` selection unambiguous, so the invariant
    can be stated against *the* picked head without replaying the pick logic here.
    """
    local_hash = draw(_hashes)
    local_file = {
        "filename": "Game.srm",
        "path": "/tmp/Game.srm",
        "size": draw(_sizes),
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
        "files_state": draw(_files_states),
        "local_hash": local_hash,
    }


@given(scenario=_identical_content_scenario())
def test_identical_content_never_conflicts(scenario: dict[str, Any]) -> None:
    """Branch 5 / #1480 (also re-verified at branches 6 and 5's no-baseline
    slice) — whenever ``local_hash`` and the picked head's ``content_hash`` are
    both truthy and EQUAL, the kernel never returns ``Conflict``. Two independent
    moves that land on identical bytes have nothing to reconcile, so the safe
    outcome is always a ``Download`` (adopt the head) or a ``Skip`` (adopt the
    baseline) — the row-12 split (12a) stated directly.

    The single documented carve-out is the branch-4 (``is_current=true``)
    size-corruption guard (#1062 / row 9b): a 0-byte or truncated local that
    happens to hash-match an equally-truncated server head is still refused in
    place. That guard is size-based, orthogonal to content identity — so the ONLY
    identical-content ``Conflict`` the kernel may emit is that shrink-guarded one.
    """
    local_file = scenario["local_file"]
    server = scenario["server"]
    files_state = scenario["files_state"]
    local_hash = scenario["local_hash"]

    result = _action(local_file, [server], files_state, local_hash)

    if isinstance(result, Conflict):
        # Must be the branch-4 shrink guard and nothing else.
        our_entry = next(
            (d for d in server["device_syncs"] if d["device_id"] == DEVICE_ID),
            None,
        )
        assert our_entry is not None and our_entry["is_current"] is True
        # Divergence from a held baseline is the precondition for reaching the guard.
        assert files_state.get("last_sync_hash") not in (None, local_hash)
        assert is_implausibly_shrunken(local_file["size"], files_state.get("last_sync_local_size"))
