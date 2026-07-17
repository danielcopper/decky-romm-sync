"""Shared, test-only model of RomM 4.9.2 save-sync server semantics.

Both ``FakeSaveApi`` and ``FakeRommApi`` import these helpers so the two
fakes model one server contract without duplicating (and drifting) the
rules. Everything here is pure: no I/O, no state — inputs in, a value or a
raised ``RommConflictError`` out.

Modeled from the verified RomM 4.9.2 behavior:

- ``is_current`` on a save's ``device_syncs`` entry is
  ``last_synced_at >= save.updated_at`` (``>=``, not ``>``): a device is
  current once its recorded sync is at or after the save's last change.
- A slot POST tags the stored filename with a timestamp marker
  (``Name [YYYY-MM-DD_HH-MM-SS].ext``) so slot versions never collide.
- ``add_save`` refuses (409) an ``overwrite=false`` POST that would stack
  onto a slot whose latest save the calling device has not synced —
  including a device that has NEVER synced a non-empty slot.

Keeping these three rules in one place is the point: before this module a
fake's ``upload_save`` unconditionally succeeded and its ``list_saves``
stamped ``is_current=True`` for any querying device, so a test could not
tell a working 409 backstop from a missing one.
"""

from __future__ import annotations

from typing import Any

from lib.errors import RommConflictError


def compute_is_current(last_synced_at: str | None, save_updated_at: str) -> bool:
    """Whether a device's sync is current for a save (``last_synced_at >= updated_at``).

    A device that has never synced the save (``last_synced_at`` is None) is
    never current. ISO-8601 timestamps compare correctly as strings when they
    share the same zone/offset, which the fakes always produce (UTC ``Z`` /
    ``+00:00`` ISO output).
    """
    if last_synced_at is None:
        return False
    return last_synced_at >= save_updated_at


def with_absent_device_placeholder(device_syncs: list[dict[str, Any]], device_id: str | None) -> list[dict[str, Any]]:
    """Ensure *device_id* appears in *device_syncs* as ``is_current=false``.

    Models ``add_save``'s content-dedup early-return response (saves.py:67-76):
    when the uploading device has no DeviceSaveSync row on the matched save, RomM
    synthesizes an ``is_current=false`` placeholder for it. Existing rows (a stale
    prior sync) pass through untouched — either way the uploading device reads as
    not current, which is the discriminator the post-upload confirm keys on (#1458).
    """
    if device_id and not any(ds.get("device_id") == device_id for ds in device_syncs):
        return [*device_syncs, {"device_id": device_id, "is_current": False, "last_synced_at": None}]
    return device_syncs


def tag_filename(filename: str, ts: str) -> str:
    """Insert a ``[ts]`` marker before the extension (slot POST filename tagging).

    ``("pokemon.srm", "2026-02-17_06-00-00")`` -> ``"pokemon [2026-02-17_06-00-00].srm"``.
    A filename with no extension (or a leading-dot dotfile, where ``rfind``
    lands at index 0) gets the marker appended: ``"save"`` -> ``"save [ts]"``.
    """
    dot = filename.rfind(".")
    if dot <= 0:
        return f"{filename} [{ts}]"
    return f"{filename[:dot]} [{ts}]{filename[dot:]}"


def check_add_save_conflict(
    *,
    device_id: str | None,
    slot: str | None,
    overwrite: bool,
    slot_saves: list[dict[str, Any]],
    sync_ledger: dict[tuple[str, int], str],
) -> None:
    """Raise ``RommConflictError`` if an ``overwrite=false`` slot POST would stack unsafely.

    Mirrors the RomM 4.9.2 ``add_save`` gate: with a device and a slot, the
    server takes the latest save in the slot and refuses the create unless the
    calling device has synced that save to its current version
    (``last_synced_at >= latest.updated_at``). A device with no sync row for
    the latest save (never synced this non-empty slot) is refused too — the
    ``compute_is_current`` False branch covers both the missing-row and the
    stale-row case.

    A first save into an empty slot, an ``overwrite=true`` POST, or a POST
    with no device/slot is always allowed — there is nothing to stack on.
    """
    if overwrite or not device_id or not slot or not slot_saves:
        return
    latest = max(slot_saves, key=lambda s: s.get("updated_at", ""))
    latest_id = latest.get("id")
    latest_updated_at = latest.get("updated_at", "")
    last_synced_at = sync_ledger.get((device_id, latest_id)) if latest_id is not None else None
    if not compute_is_current(last_synced_at, latest_updated_at):
        raise RommConflictError(
            "Save slot has a newer server version not yet synced to this device",
            method="POST",
        )
