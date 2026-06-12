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
# RetroArch ``savefiles_in_content_dir=true``: saves are written next to the
# ROM, outside the saves tree the plugin syncs, so save sync is unavailable.
# Neutral phrasing — the frontend treats this as a benign skip, not an error.
SAVE_SYNC_IN_CONTENT_DIR = "Save sync is unavailable: RetroArch is set to write saves to the content directory."
# ``reason`` slug on the sync-gate failure shape; the frontend routes on this to
# treat the result as a skip (no error, launch proceeds), not a failure. Single
# source of truth is ``domain.save_layout`` — re-exported here so the saves
# service code keeps importing it from its own message module.
SAVE_SYNC_IN_CONTENT_DIR_REASON = SAVE_SYNC_CONTENT_DIR_REASON
