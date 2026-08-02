"""User-facing message constants for the saves package.

All status and error message strings returned in the ``message`` /
``error`` fields of save-sync API responses live here so they stay
consistent across modules. Add to this file rather than inlining new
literals in service code.
"""

from domain.save_layout import SAVE_SYNC_CONTENT_DIR_REASON

SAVE_SYNC_DISABLED = "Save sync is disabled"
DEVICE_NOT_REGISTERED = "Device not registered"
# Bespoke ``reason`` slugs for the canonical failure shape on the save-sync
# guard returns. Plain strings (not :class:`ErrorCode`) — these are
# domain-specific skip/guard categories, not server-reachability failures.
SAVE_SYNC_DISABLED_REASON = "sync_disabled"
DEVICE_NOT_REGISTERED_REASON = "device_not_registered"
# Wizard legacy-migration precheck: no device is registered yet, so the migration
# can't upload into the slot. Refused before any local/server mutation so the
# wizard just stays open and the user can retry (#1498 review).
MIGRATION_DEVICE_NOT_REGISTERED = (
    "This device isn't registered with RomM yet — retry in a moment (it registers automatically on the next save sync)."
)
# RomM's per-device ``sync_enabled`` switch is off server-side (the negotiate 400
# policy stop, #1489). Distinct from ``sync_disabled``, which is the LOCAL toggle.
DEVICE_SYNC_DISABLED_REASON = "device_sync_disabled"
DEVICE_SYNC_DISABLED = "Save sync is disabled for this device on the RomM server"
# The device-wide save-sync gate was still held by another run when the bounded
# wait expired, so this run was skipped. A LOCAL scheduling outcome — it says
# nothing about the server, which the skipped run may never have contacted
# (#1625). Never collapse it onto ``server_unreachable``.
SAVE_SYNC_BUSY_REASON = "sync_busy"
SAVE_SYNC_BUSY = "Another save sync is still running"
# RetroArch ``savefiles_in_content_dir=true``: saves are written next to the
# ROM, outside the saves tree the plugin syncs, so save sync is unavailable.
# Neutral phrasing — the frontend treats this as a benign skip, not an error.
SAVE_SYNC_IN_CONTENT_DIR = "Save sync is unavailable: RetroArch is set to write saves to the content directory."
# ``reason`` slug on the sync-gate failure shape; the frontend routes on this to
# treat the result as a skip (no error, launch proceeds), not a failure. Single
# source of truth is ``domain.save_layout`` — re-exported here so the saves
# service code keeps importing it from its own message module.
SAVE_SYNC_IN_CONTENT_DIR_REASON = SAVE_SYNC_CONTENT_DIR_REASON
